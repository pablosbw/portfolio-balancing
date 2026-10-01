"""
Rebalance the dummy user's portfolio: place (filled) orders and update holdings and cash.

    python manage.py rebalance
    python manage.py rebalance --threshold 0.05
    python manage.py rebalance --dry-run      # only show the plan
"""

from decimal import Decimal

from django.core.management.base import BaseCommand

from portfolio.models import DUMMY_USER_ID, Portfolio


class Command(BaseCommand):
    help = "Sell and buy stocks to match the target allocation."

    def add_arguments(self, parser):
        parser.add_argument(
            "--threshold", type=Decimal, default=Decimal("0.01"),
            help="Minimum drift to trade, as a fraction of total value (default 0.01 = 1%%).",
        )
        parser.add_argument("--dry-run", action="store_true", help="Show the plan without placing orders.")

    def handle(self, *args, threshold, dry_run, **options):
        portfolio = Portfolio.objects.get(user_id=DUMMY_USER_ID)
        plan = portfolio.rebalance(threshold=threshold, dry_run=dry_run)

        self.stdout.write(f"Total value: {plan.total_value:,.2f}  (cash {plan.cash_before:,.2f})")
        self.stdout.write(f"Threshold:   {threshold:.2%}\n")
        self.stdout.write(f"{'Asset':<6} {'Current':>8} {'Target':>8} {'Diff':>8}")
        for d in sorted(plan.drifts, key=lambda d: d.symbol):
            self.stdout.write(
                f"{d.symbol:<6} {d.current_weight:>8.2%} {d.target_weight:>8.2%} {d.diff:>+8.2%}"
            )

        self.stdout.write("")
        if not plan.trades:
            self.stdout.write("Nothing to do: every drift is below the threshold.")
        for t in plan.trades:
            self.stdout.write(f"{t.side:<4} {t.quantity:>16} {t.symbol:<6} @ {t.price:,.2f} = {t.amount:,.2f}")
        self.stdout.write(f"\nCash after rebalance: {plan.cash_after:,.2f}")

        if dry_run:
            self.stdout.write(self.style.WARNING("\nDry run: no orders placed."))
        elif plan.trades:
            self.stdout.write(self.style.SUCCESS(
                f"\nPlaced {len(plan.trades)} orders. See them with: python manage.py orders"
            ))
