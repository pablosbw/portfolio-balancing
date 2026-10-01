"""
Seed the database with sample stocks, orders and a single portfolio.

IDs are derived deterministically (UUIDv5), so running it again updates the same
rows instead of creating duplicates. A run resets the sample data to its initial
state: prices, cash and holdings, and it deletes the orders a rebalance created.

    python manage.py seed             # reset sample data
    python manage.py seed --if-empty  # only seed a fresh database (used by docker-compose)

Prices are illustrative sample values, not live market data.
"""

import uuid
from collections import defaultdict
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from portfolio.models import DUMMY_USER_ID, Order, Portfolio, Stock

SEED_NAMESPACE = uuid.UUID("6f1c2a52-4c1e-4b8e-9a43-0d5a3f6f1e01")

STOCKS = [
    # symbol, market, company name, divisible, current price
    ("AAPL", "NASDAQ", "Apple Inc.", True, "230.50000000"),
    ("META", "NASDAQ", "Meta Platforms, Inc.", True, "720.25000000"),
    ("MSFT", "NASDAQ", "Microsoft Corporation", True, "510.75000000"),
    ("GOOGL", "NASDAQ", "Alphabet Inc. Class A", True, "245.10000000"),
    ("BRK.A", "NYSE", "Berkshire Hathaway Inc. Class A", False, "740000.00000000"),
]

# Order history that produced the portfolio's current holdings.
# (key, symbol, side, status, proposed qty, proposed price, executed qty, executed price)
ORDERS = [
    ("o1", "AAPL", Order.Side.BUY, Order.Status.FILLED, "10", "180", "10", "180"),
    ("o2", "AAPL", Order.Side.BUY, Order.Status.FILLED, "5", "210", "5", "210"),
    ("o3", "META", Order.Side.BUY, Order.Status.FILLED, "8", "500", "8", "500"),
    ("o4", "MSFT", Order.Side.BUY, Order.Status.PARTIALLY_FILLED, "6", "400", "4", "399.5"),
    ("o5", "GOOGL", Order.Side.BUY, Order.Status.CANCELED, "3", "170", "0", None),
    ("o6", "AAPL", Order.Side.SELL, Order.Status.PENDING, "2", "235", "0", None),
]

# Target allocation from the task statement: 40% META / 60% AAPL.
# Current holdings include MSFT (not in the target), so a rebalance has work to do.
EXPECTED_COMPOSITION = [("META", "0.4"), ("AAPL", "0.6")]

# Uninvested cash, e.g. a recent deposit the rebalance can deploy.
PORTFOLIO_CASH = "1000.00000000"


def seed_id(kind: str, key: str) -> uuid.UUID:
    return uuid.uuid5(SEED_NAMESPACE, f"{kind}:{key}")


class Command(BaseCommand):
    help = "Seed stocks, orders and a sample portfolio (resets them if they exist)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--if-empty", action="store_true", help="Do nothing if there are already stocks in the database."
        )

    @transaction.atomic
    def handle(self, *args, if_empty, **options):
        if if_empty and Stock.objects.exists():
            self.stdout.write("Database already seeded, skipping.")
            return
        stocks = self._seed_stocks()
        orders = self._seed_orders(stocks)
        portfolio = self._seed_portfolio(stocks, orders)

        # Link the sample orders to the portfolio, and drop any order created
        # since (e.g. by a rebalance), so orders and holdings stay consistent.
        seeded_ids = [o.pk for o in orders]
        Order.objects.filter(pk__in=seeded_ids).update(portfolio=portfolio)
        Order.objects.filter(portfolio=portfolio).exclude(pk__in=seeded_ids).delete()
        self.stdout.write(self.style.SUCCESS(
            f"Seeded {len(stocks)} stocks, {len(orders)} orders, portfolio {portfolio.portfolio_id}."
        ))

    def _seed_stocks(self) -> dict[str, Stock]:
        stocks = {}
        for symbol, market, name, divisible, price in STOCKS:
            stock, _ = Stock.objects.update_or_create(
                asset_id=seed_id("stock", f"{market}:{symbol}"),
                defaults={
                    "symbol": symbol,
                    "market": market,
                    "company_name": name,
                    "currency": "USD",
                    "is_divisible": divisible,
                    "current_price": Decimal(price),
                },
            )
            stocks[symbol] = stock
        return stocks

    def _seed_orders(self, stocks: dict[str, Stock]) -> list[Order]:
        orders = []
        for key, symbol, side, status, p_qty, p_price, e_qty, e_price in ORDERS:
            order, _ = Order.objects.update_or_create(
                order_id=seed_id("order", key),
                defaults={
                    "user_id": DUMMY_USER_ID,
                    "asset": stocks[symbol],
                    "side": side,
                    "status": status,
                    "proposed_quantity": Decimal(p_qty),
                    "proposed_price": Decimal(p_price),
                    "executed_quantity": Decimal(e_qty),
                    "executed_price": Decimal(e_price) if e_price is not None else None,
                },
            )
            orders.append(order)
        return orders

    def _seed_portfolio(self, stocks: dict[str, Stock], orders: list[Order]) -> Portfolio:
        portfolio, _ = Portfolio.objects.update_or_create(
            portfolio_id=seed_id("portfolio", DUMMY_USER_ID),
            defaults={
                "user_id": DUMMY_USER_ID,
                "currency": "USD",
                "cash": Decimal(PORTFOLIO_CASH),
                "expected_composition": [
                    {"assetId": str(stocks[symbol].asset_id), "weight": weight}
                    for symbol, weight in EXPECTED_COMPOSITION
                ],
                "current_composition": holdings_from_orders(orders),
            },
        )
        return portfolio


def holdings_from_orders(orders: list[Order]) -> list[dict]:
    """
    Derive positions from executed quantities, so holdings always match the order history.

    ``price`` is the average acquisition cost of the BUY executions. SELLs reduce
    the quantity but not the average cost (standard average-cost method).
    """
    quantity = defaultdict(Decimal)
    bought_qty = defaultdict(Decimal)
    bought_cost = defaultdict(Decimal)

    for order in orders:
        if order.executed_quantity == 0:
            continue
        asset_id = str(order.asset_id)
        if order.side == Order.Side.BUY:
            quantity[asset_id] += order.executed_quantity
            bought_qty[asset_id] += order.executed_quantity
            bought_cost[asset_id] += order.executed_quantity * order.executed_price
        else:
            quantity[asset_id] -= order.executed_quantity

    exp = Decimal(10) ** -8
    return [
        {
            "assetId": asset_id,
            "quantity": str(qty.quantize(exp)),
            "price": str((bought_cost[asset_id] / bought_qty[asset_id]).quantize(exp)),
        }
        for asset_id, qty in quantity.items()
        if qty > 0
    ]
