from decimal import Decimal
from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase

from .helpers import make_stock


class UpdatePricesCommandTests(TestCase):
    def setUp(self):
        self.aaa = make_stock("AAA", "100")
        self.brk = make_stock("BRK.A", "740000", is_divisible=False)

    def run_command(self, *args):
        call_command("update_prices", *args, stdout=StringIO())
        self.aaa.refresh_from_db()
        self.brk.refresh_from_db()

    def test_random_move_stays_within_one_percent(self):
        for seed in range(20):
            before = {"AAA": self.aaa.current_price, "BRK.A": self.brk.current_price}
            self.run_command("--seed", str(seed))
            for stock in (self.aaa, self.brk):
                ratio = stock.current_price / before[stock.symbol]
                self.assertTrue(Decimal("0.99") <= ratio <= Decimal("1.01"), (stock.symbol, ratio))

    def test_override_only_changes_listed_stocks(self):
        self.run_command("BRK.A,0.9")
        self.assertEqual(self.brk.current_price, Decimal("666000"))
        self.assertEqual(self.aaa.current_price, Decimal("100"))

    def test_unknown_symbol_or_bad_factor_fails(self):
        for args in (["NOPE,0.9"], ["AAA,0"], ["AAA,abc"]):
            with self.subTest(args=args), self.assertRaises(CommandError):
                self.run_command(*args)
