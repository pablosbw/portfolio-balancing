"""
Portfolio rebalancing algorithm.

This module is pure: it gets plain values (cash, prices, quantities, target
weights) and returns a plan. It doesn't touch the database or place orders,
which keeps the maths easy to follow and to test. ``Portfolio.rebalance()``
gathers the data and calls ``plan_rebalance()``.

Algorithm
---------
1. Total value = cash + sum(quantity * current price) over every held asset.
   Current market price is used, not the acquisition price, because what we
   can sell for (or must pay) is today's price.
2. For each asset in the target and/or holdings:
       target value  = target weight * total value
       diff value    = target value - current value
       diff weight   = diff value / total value
   An asset that is held but not in the target has target weight 0, so it is
   fully sold.
3. If |diff weight| < threshold the asset is "close enough" and left alone.
   This avoids paying fees/spread on tiny trades. The threshold is a fraction
   of total value: 0.01 means 1%.
4. Sells first: every overweight asset sells the excess. The proceeds join
   the available cash.
5. Buys with that cash, most underweight asset first (largest diff weight),
   so if money runs short the biggest gaps are closed first.
6. Before each buy, check how much cash is left. Skipped sells (step 3) mean
   cash may not cover every buy; in that case the buy is reduced to what is
   affordable, or skipped if nothing is.

Quantities are always rounded DOWN, never up:
* divisible assets to 8 decimals (the DB precision),
* indivisible assets (e.g. BRK.A) to whole units.
Rounding down guarantees we never sell more than the excess and never spend
more than the cash available. The leftover stays as cash.
"""

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

BUY = "BUY"
SELL = "SELL"

QUANTITY_STEP = Decimal("0.00000001")
WHOLE_UNIT = Decimal("1")


@dataclass(frozen=True)
class AssetPosition:
    """Everything the algorithm needs to know about one asset."""

    asset_id: str
    symbol: str
    price: Decimal
    is_divisible: bool
    quantity: Decimal  # Currently held (0 if only in the target).
    target_weight: Decimal  # 0 if only held, not in the target.


@dataclass(frozen=True)
class Drift:
    """How far an asset is from its target, as a fraction of total value."""

    asset_id: str
    symbol: str
    current_weight: Decimal
    target_weight: Decimal

    @property
    def diff(self) -> Decimal:
        """Positive: underweight (buy). Negative: overweight (sell)."""
        return self.target_weight - self.current_weight


@dataclass(frozen=True)
class Trade:
    asset_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal

    @property
    def amount(self) -> Decimal:
        return self.quantity * self.price


@dataclass(frozen=True)
class RebalancePlan:
    total_value: Decimal
    cash_before: Decimal
    cash_after: Decimal
    drifts: list[Drift]
    # Sells first, then buys in execution (priority) order.
    trades: list[Trade]

    @property
    def sells(self) -> list[Trade]:
        return [t for t in self.trades if t.side == SELL]

    @property
    def buys(self) -> list[Trade]:
        return [t for t in self.trades if t.side == BUY]


def plan_rebalance(cash: Decimal, positions: list[AssetPosition], threshold: Decimal) -> RebalancePlan:
    if threshold < 0:
        raise ValueError("threshold must be >= 0")

    # Step 1: total value of the portfolio.
    total = cash + sum((p.quantity * p.price for p in positions), Decimal("0"))
    if total <= 0:
        return RebalancePlan(total, cash, cash, drifts=[], trades=[])

    drifts: list[Drift] = []
    sells: list[Trade] = []
    buy_candidates: list[tuple[Decimal, Decimal, AssetPosition]] = []  # (diff weight, diff value, position)

    for p in positions:
        # Step 2: distance to the target.
        current_value = p.quantity * p.price
        diff_value = p.target_weight * total - current_value
        diff_weight = diff_value / total
        drifts.append(Drift(p.asset_id, p.symbol, current_value / total, p.target_weight))

        # Step 3: ignore drifts below the threshold.
        if diff_value == 0 or abs(diff_weight) < threshold:
            continue

        if diff_value < 0:
            # Step 4: sell the excess. Never more than what is held.
            quantity = min(_tradable_quantity(-diff_value / p.price, p.is_divisible), p.quantity)
            if quantity > 0:
                sells.append(Trade(p.asset_id, p.symbol, SELL, quantity, p.price))
        else:
            buy_candidates.append((diff_weight, diff_value, p))

    available_cash = cash + sum((t.amount for t in sells), Decimal("0"))

    # Step 5: most underweight first. Symbol breaks ties so the result is deterministic.
    buy_candidates.sort(key=lambda c: (-c[0], c[2].symbol))
    buys: list[Trade] = []
    for _, diff_value, p in buy_candidates:
        # Step 6: buy what is needed, capped by the cash we actually have.
        budget = min(diff_value, available_cash)
        quantity = _tradable_quantity(budget / p.price, p.is_divisible)
        if quantity <= 0:
            continue
        trade = Trade(p.asset_id, p.symbol, BUY, quantity, p.price)
        available_cash -= trade.amount
        buys.append(trade)

    return RebalancePlan(total, cash, available_cash, drifts, sells + buys)


def _tradable_quantity(quantity: Decimal, is_divisible: bool) -> Decimal:
    step = QUANTITY_STEP if is_divisible else WHOLE_UNIT
    return quantity.quantize(step, rounding=ROUND_DOWN)
