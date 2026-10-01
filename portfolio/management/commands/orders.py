"""
List the orders placed for the dummy user's portfolio, newest first, and its current holdings.

    python manage.py orders
    python manage.py orders --status FILLED --limit 10
"""

from decimal import Decimal

from django.core.management.base import BaseCommand

from portfolio.models import DUMMY_USER_ID, Order, Portfolio, Stock


class Command(BaseCommand):
    help = "List a portfolio's orders and current holdings."

    def add_arguments(self, parser):
        parser.add_argument("--status", choices=Order.Status.values, help="Only orders with this status.")
        parser.add_argument("--limit", type=int, default=50, help="Max number of orders (default 50).")

    def handle(self, *args, status, limit, **options):
        portfolio = Portfolio.objects.get(user_id=DUMMY_USER_ID)
        orders = portfolio.orders.select_related("asset").order_by("-created_at")
        if status:
            orders = orders.filter(status=status)

        self.stdout.write(f"Orders of portfolio {portfolio.portfolio_id}\n")
        self.stdout.write(
            f"{'Created (UTC)':<19}  {'Side':<4} {'Asset':<6} {'Status':<16} "
            f"{'Proposed qty':>16} {'Proposed price':>16} {'Executed qty':>16} {'Executed price':>16}"
        )
        for o in orders[:limit]:
            self.stdout.write(
                f"{o.created_at:%Y-%m-%d %H:%M:%S}  {o.side:<4} {o.asset.symbol:<6} {o.status:<16} "
                f"{o.proposed_quantity:>16f} {o.proposed_price:>16f} "
                f"{o.executed_quantity:>16f} {'-' if o.executed_price is None else f'{o.executed_price:f}':>16}"
            )

        asset_ids = [e["assetId"] for e in portfolio.current_composition]
        stocks = {str(s.pk): s for s in Stock.objects.filter(asset_id__in=asset_ids)}
        self.stdout.write(f"\nHoldings (cash {portfolio.cash:,.2f})")
        for e in portfolio.current_composition:
            stock = stocks[str(e["assetId"])]
            value = Decimal(e["quantity"]) * stock.get_current_price()
            self.stdout.write(
                f"{stock.symbol:<6} {Decimal(e['quantity']):>16}  "
                f"avg cost {Decimal(e['price']):>14,.2f}  value {value:>12,.2f}"
            )
        self.stdout.write(f"Total value: {portfolio.total_value():,.2f}")
