"""
Simulate market movement by changing stock prices.

    python manage.py update_prices                  # every stock moves randomly in [-1%, +1%]
    python manage.py update_prices BRK.A,0.9        # only BRK.A, price * 0.9 (a 10% drop)
    python manage.py update_prices BRK.A,0.9 AAPL   # BRK.A * 0.9, AAPL random; the rest unchanged
    python manage.py update_prices --seed 42        # reproducible random moves

Each override is SYMBOL[,FACTOR]. FACTOR multiplies the current price and must be > 0.
Once any override is given, only the listed stocks change.
"""

import random
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from portfolio.models import PRICE_DECIMAL_PLACES, Stock

MAX_RANDOM_MOVE_BPS = 100  # 100 basis points = 1%
PRICE_STEP = Decimal(1).scaleb(-PRICE_DECIMAL_PLACES)


class Command(BaseCommand):
    help = "Move stock prices randomly (±1%) or by explicit factors (e.g. BRK.A,0.9)."

    def add_arguments(self, parser):
        parser.add_argument("overrides", nargs="*", metavar="SYMBOL[,FACTOR]")
        parser.add_argument("--seed", type=int, help="Random seed, for reproducible moves.")

    @transaction.atomic
    def handle(self, *args, overrides, seed, **options):
        rng = random.Random(seed)
        factors = self._parse_overrides(overrides)

        stocks = Stock.objects.select_for_update()
        if factors:
            stocks = stocks.filter(symbol__in=factors)
            missing = set(factors) - {s.symbol for s in stocks}
            if missing:
                raise CommandError(f"Unknown symbol(s): {', '.join(sorted(missing))}")

        for stock in stocks:
            factor = factors.get(stock.symbol) or random_factor(rng)
            old_price = stock.current_price
            stock.current_price = (old_price * factor).quantize(PRICE_STEP, rounding=ROUND_HALF_EVEN)
            if stock.current_price <= 0:
                raise CommandError(f"{stock.symbol}: price would become {stock.current_price}.")
            stock.save(update_fields=["current_price"])
            self.stdout.write(f"{stock.symbol:<6} {old_price:>18} -> {stock.current_price:>18}  (x{factor})")

    def _parse_overrides(self, overrides: list[str]) -> dict[str, Decimal | None]:
        """Map symbol -> factor; ``None`` means "random move for this symbol"."""
        factors = {}
        for raw in overrides:
            symbol, _, factor = (part.strip() for part in raw.partition(","))
            if not symbol:
                raise CommandError(f"Invalid override {raw!r}: expected SYMBOL[,FACTOR].")
            if not factor:
                factors[symbol] = None
                continue
            try:
                factors[symbol] = Decimal(factor)
            except InvalidOperation:
                raise CommandError(f"Invalid factor for {symbol}: {factor!r}.")
            if factors[symbol] <= 0:
                raise CommandError(f"Factor for {symbol} must be > 0.")
        return factors


def random_factor(rng: random.Random) -> Decimal:
    """Uniform factor in [0.99, 1.01], in whole basis points so it is an exact Decimal."""
    return 1 + Decimal(rng.randint(-MAX_RANDOM_MOVE_BPS, MAX_RANDOM_MOVE_BPS)) / 10_000
