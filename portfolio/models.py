"""
Domain models for the portfolio module.

Design notes
------------
* Money and quantities use ``Decimal`` (never ``float``) to avoid binary rounding
  errors. Prices are stored as NUMERIC(16, 8): up to 16 digits, 8 of them decimals,
  i.e. values up to 99,999,999.99999999. Quantities use the same precision so we can
  represent fractional shares.
* Primary keys are UUIDs so identifiers are safe to expose and can be generated
  outside the database (e.g. deterministically by the seed command).
* There is no authentication yet: every row belongs to the constant ``DUMMY_USER_ID``.
  The column exists (and is indexed) so filtering by user is trivial later on.
* Python attributes are snake_case (Django/PostgreSQL convention). The JSON payloads
  stored in ``Portfolio`` use camelCase keys (``assetId``, ``weight``...) since they
  are the shape we expect to expose through an API.
"""

import uuid
from decimal import ROUND_DOWN, Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import models, transaction

from .rebalance import BUY, AssetPosition, RebalancePlan, plan_rebalance

DUMMY_USER_ID = "dummy"

PRICE_MAX_DIGITS = 16
PRICE_DECIMAL_PLACES = 8
DECIMAL_STEP = Decimal(1).scaleb(-PRICE_DECIMAL_PLACES)


def _decimal_field(**kwargs) -> models.DecimalField:
    return models.DecimalField(
        max_digits=PRICE_MAX_DIGITS, decimal_places=PRICE_DECIMAL_PLACES, **kwargs
    )


class Stock(models.Model):
    """A tradable asset with its last known price (no price history is kept)."""

    asset_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    symbol = models.CharField(max_length=16)
    market = models.CharField(max_length=16)
    company_name = models.CharField(max_length=255)
    currency = models.CharField(max_length=3, default="USD")
    # Whether fractional units can be traded. Some assets (e.g. BRK.A) are
    # typically only traded in whole units, which matters when rebalancing.
    is_divisible = models.BooleanField(default=True)
    current_price = _decimal_field()

    class Meta:
        ordering = ["symbol"]
        constraints = [
            # The same ticker can exist in different markets, but not twice in one.
            models.UniqueConstraint(fields=["symbol", "market"], name="uniq_stock_symbol_market"),
            models.CheckConstraint(condition=models.Q(current_price__gt=0), name="stock_price_positive"),
        ]

    def __str__(self) -> str:
        return f"{self.symbol} ({self.market})"

    def get_current_price(self) -> Decimal:
        """Last available price. Today it is the stored column; later it could call a market-data provider."""
        return self.current_price


class Order(models.Model):
    """A request to buy or sell an asset, and how much of it was actually executed."""

    class Side(models.TextChoices):
        BUY = "BUY", "Buy"
        SELL = "SELL", "Sell"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PARTIALLY_FILLED = "PARTIALLY_FILLED", "Partially filled"
        CANCELED = "CANCELED", "Canceled"
        FILLED = "FILLED", "Filled"

    order_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_id = models.CharField(max_length=64, default=DUMMY_USER_ID, db_index=True)
    # Portfolio the order belongs to (e.g. created by a rebalance). Nullable so an
    # order can exist on its own; PROTECT keeps the order history if someone tries
    # to delete the portfolio.
    portfolio = models.ForeignKey(
        "Portfolio", on_delete=models.PROTECT, null=True, blank=True, related_name="orders"
    )
    # Column is named ``asset_id``; PROTECT avoids deleting a stock that has orders.
    asset = models.ForeignKey(Stock, on_delete=models.PROTECT, related_name="orders")
    side = models.CharField(max_length=4, choices=Side.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    proposed_quantity = _decimal_field()
    proposed_price = _decimal_field()
    executed_quantity = _decimal_field(default=Decimal("0"))
    # Average execution price; null until something gets executed.
    executed_price = _decimal_field(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=models.Q(proposed_quantity__gt=0), name="order_proposed_qty_positive"),
            models.CheckConstraint(condition=models.Q(proposed_price__gt=0), name="order_proposed_price_positive"),
            models.CheckConstraint(
                condition=models.Q(executed_quantity__gte=0)
                & models.Q(executed_quantity__lte=models.F("proposed_quantity")),
                name="order_executed_qty_in_range",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.side} {self.proposed_quantity} {self.asset_id} [{self.status}]"


class Portfolio(models.Model):
    """
    A user's portfolio: what it should look like vs. what it currently holds.

    ``expected_composition`` - target allocation, e.g. 40% META / 60% AAPL::

        [{"assetId": "<uuid>", "weight": "0.4"}, {"assetId": "<uuid>", "weight": "0.6"}]

    ``current_composition`` - actual holdings and their average acquisition price::

        [{"assetId": "<uuid>", "quantity": "15", "price": "190.00000000"}]

    Numbers are stored as strings in JSON so they round-trip to ``Decimal``
    without losing precision. Market value is computed with
    ``Stock.get_current_price()``, not with the stored acquisition price.

    JSON (instead of join tables) keeps a portfolio a single document that is
    read and written atomically; integrity is enforced in ``clean()``.

    ``cash`` - uninvested balance in ``currency``. It is part of the portfolio's
    total value, so target weights apply to ``holdings + cash``: a rebalance can
    deploy idle cash, and sells free up cash that buys then consume.
    """

    portfolio_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_id = models.CharField(max_length=64, default=DUMMY_USER_ID, db_index=True)
    currency = models.CharField(max_length=3, default="USD")
    cash = _decimal_field(default=Decimal("0"))
    expected_composition = models.JSONField(default=list, blank=True)
    current_composition = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(cash__gte=0),
                name="portfolio_cash_non_negative",
                violation_error_message="Cash cannot be negative.",
            ),
        ]

    def __str__(self) -> str:
        return f"Portfolio {self.portfolio_id} ({self.user_id})"

    def holdings_value(self) -> Decimal:
        """Market value of current holdings, using each stock's latest price."""
        quantities = {str(e["assetId"]): Decimal(str(e["quantity"])) for e in self.current_composition}
        stocks = Stock.objects.filter(asset_id__in=quantities)
        return sum((quantities[str(s.pk)] * s.get_current_price() for s in stocks), Decimal("0"))

    def total_value(self) -> Decimal:
        """Holdings at market value plus cash: the base that target weights apply to."""
        return self.holdings_value() + self.cash

    def rebalance(self, threshold: Decimal = Decimal("0.01"), dry_run: bool = False) -> RebalancePlan:
        """
        Sell and buy stocks so the portfolio matches ``expected_composition``.

        ``threshold`` is the minimum drift (fraction of total value, 0.01 = 1%)
        worth trading. See ``portfolio/rebalance.py`` for the algorithm.

        Unless ``dry_run`` is set, the plan is executed in one transaction:
        one ``Order`` per trade is created, and ``cash`` and
        ``current_composition`` are updated. There is no broker yet, so orders
        are filled immediately, at the proposed quantity and price.
        Returns the plan in both cases.
        """
        with transaction.atomic():
            if not dry_run:
                # Lock the row: two concurrent rebalances must not spend the same cash.
                Portfolio.objects.select_for_update().get(pk=self.pk)
                self.refresh_from_db()
            plan = self._plan_rebalance(Decimal(str(threshold)))
            if not dry_run and plan.trades:
                self._execute(plan)
        return plan

    def _plan_rebalance(self, threshold: Decimal) -> RebalancePlan:
        targets = {str(e["assetId"]): Decimal(str(e["weight"])) for e in self.expected_composition}
        quantities = {str(e["assetId"]): Decimal(str(e["quantity"])) for e in self.current_composition}
        stocks = Stock.objects.filter(asset_id__in=targets.keys() | quantities.keys())
        positions = [
            AssetPosition(
                asset_id=str(stock.pk),
                symbol=stock.symbol,
                price=stock.get_current_price(),
                is_divisible=stock.is_divisible,
                quantity=quantities.get(str(stock.pk), Decimal("0")),
                target_weight=targets.get(str(stock.pk), Decimal("0")),
            )
            for stock in stocks
        ]
        return plan_rebalance(self.cash, positions, threshold)

    def _execute(self, plan: RebalancePlan) -> None:
        Order.objects.bulk_create([
            Order(
                user_id=self.user_id,
                portfolio=self,
                asset_id=trade.asset_id,
                side=trade.side,
                status=Order.Status.FILLED,
                proposed_quantity=trade.quantity,
                proposed_price=trade.price,
                executed_quantity=trade.quantity,
                executed_price=trade.price,
            )
            for trade in plan.trades
        ])

        # asset_id -> (quantity, average acquisition price)
        holdings = {
            str(e["assetId"]): (Decimal(str(e["quantity"])), Decimal(str(e["price"])))
            for e in self.current_composition
        }
        for trade in plan.trades:
            quantity, avg_price = holdings.get(trade.asset_id, (Decimal("0"), Decimal("0")))
            if trade.side == BUY:
                # Average-cost method: a buy blends its price into the average.
                avg_price = (quantity * avg_price + trade.amount) / (quantity + trade.quantity)
                quantity += trade.quantity
            else:
                # A sell reduces the position but keeps its average cost.
                quantity -= trade.quantity
            holdings[trade.asset_id] = (quantity, avg_price)

        self.current_composition = [
            {"assetId": asset_id, "quantity": str(quantity), "price": str(avg_price.quantize(DECIMAL_STEP))}
            for asset_id, (quantity, avg_price) in holdings.items()
            if quantity > 0  # Fully sold positions are removed.
        ]
        # Trade amounts have up to 16 decimals (8 for quantity, 8 for price).
        # The column keeps 8, so we round down: never record cash we don't have.
        self.cash = plan.cash_after.quantize(DECIMAL_STEP, rounding=ROUND_DOWN)
        self.save()

    def save(self, *args, **kwargs):
        # Always validate: the JSON columns have no DB-level schema.
        self.full_clean()
        super().save(*args, **kwargs)

    def clean(self):
        errors = {}
        try:
            self._validate_expected_composition()
        except ValidationError as e:
            errors["expected_composition"] = e.messages
        try:
            self._validate_current_composition()
        except ValidationError as e:
            errors["current_composition"] = e.messages
        if not errors:
            try:
                self._validate_currency()
            except ValidationError as e:
                errors["currency"] = e.messages
        if errors:
            raise ValidationError(errors)

    def _validate_currency(self):
        """Without FX, every asset must be priced in the portfolio's cash currency."""
        asset_ids = {str(e["assetId"]) for e in self.expected_composition + self.current_composition}
        mismatched = (
            Stock.objects.filter(asset_id__in=asset_ids)
            .exclude(currency=self.currency)
            .values_list("symbol", flat=True)
        )
        if mismatched:
            raise ValidationError(f"Assets not priced in {self.currency}: {sorted(mismatched)}.")

    def _validate_expected_composition(self):
        entries = _require_list_of_dicts(self.expected_composition, required_keys={"assetId", "weight"})
        if not entries:
            return  # An empty target is allowed (e.g. a brand-new portfolio).
        _require_unique_existing_assets(entries)

        total = Decimal("0")
        for entry in entries:
            weight = _to_decimal(entry["weight"], "weight")
            if weight < 0:
                raise ValidationError(f"Weight for {entry['assetId']} must be >= 0.")
            total += weight
        if total != Decimal("1"):
            raise ValidationError(f"Weights must sum to 1 (got {total}).")

    def _validate_current_composition(self):
        entries = _require_list_of_dicts(
            self.current_composition, required_keys={"assetId", "quantity", "price"}
        )
        _require_unique_existing_assets(entries)
        for entry in entries:
            if _to_decimal(entry["quantity"], "quantity") < 0:
                raise ValidationError(f"Quantity for {entry['assetId']} must be >= 0.")
            if _to_decimal(entry["price"], "price") < 0:
                raise ValidationError(f"Price for {entry['assetId']} must be >= 0.")


# --- validation helpers -----------------------------------------------------


def _to_decimal(value, name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError(f"Invalid {name}: {value!r}.")


def _require_list_of_dicts(value, required_keys: set[str]) -> list[dict]:
    if not isinstance(value, list):
        raise ValidationError("Must be a list.")
    for entry in value:
        if not isinstance(entry, dict) or not required_keys.issubset(entry):
            raise ValidationError(f"Each entry must be an object with keys {sorted(required_keys)}.")
    return value


def _require_unique_existing_assets(entries: list[dict]) -> None:
    asset_ids = [str(entry["assetId"]) for entry in entries]
    if len(asset_ids) != len(set(asset_ids)):
        raise ValidationError("Duplicated assetId.")
    try:
        known = {str(pk) for pk in Stock.objects.filter(asset_id__in=asset_ids).values_list("pk", flat=True)}
    except ValidationError:
        raise ValidationError("assetId must be a valid UUID.")
    missing = set(asset_ids) - known
    if missing:
        raise ValidationError(f"Unknown assetId(s): {sorted(missing)}.")
 