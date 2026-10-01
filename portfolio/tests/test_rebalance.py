"""
Rebalance tests. Prices are round numbers so every expected quantity can be
checked by hand; the arithmetic is written next to each scenario.

Prices: AAA = 100, BBB = 50, CCC = 20, DDD = 1000 (indivisible).

These tests check the plan. Tests that compare several thresholds on the same
portfolio use ``dry_run=True``, so the first run doesn't rebalance it before the
next one. Order creation is tested in ``test_rebalance_execution.py``.
"""

from decimal import Decimal

from django.test import TestCase

from portfolio.rebalance import BUY, SELL

from .helpers import make_portfolio, make_stock

D = Decimal


def trades(trade_list):
    return [(t.side, t.symbol, t.quantity) for t in trade_list]


class RebalanceTests(TestCase):
    def setUp(self):
        self.aaa = make_stock("AAA", "100")
        self.bbb = make_stock("BBB", "50")
        self.ccc = make_stock("CCC", "20")
        self.ddd = make_stock("DDD", "1000", is_divisible=False)

    def test_balanced_portfolio_needs_no_trades(self):
        # 5 AAA = 500, 10 BBB = 500 -> exactly 50/50.
        portfolio = make_portfolio(
            targets={self.aaa: "0.5", self.bbb: "0.5"}, holdings={self.aaa: "5", self.bbb: "10"}
        )
        plan = portfolio.rebalance()
        self.assertEqual(plan.total_value, D("1000"))
        self.assertEqual(plan.trades, [])

    def test_empty_portfolio_needs_no_trades(self):
        plan = make_portfolio().rebalance()
        self.assertEqual(plan.total_value, D("0"))
        self.assertEqual(plan.trades, [])

    def test_thresholds(self):
        # Holdings: 6 AAA = 600, 6 BBB = 300, 5 CCC = 100. Total 1000, no cash.
        # Target 50% AAA / 50% BBB; CCC is not in the target.
        #   AAA 60% -> 50%: -10%  (sell 100 = 1 AAA)
        #   BBB 30% -> 50%: +20%  (buy 200 = 4 BBB)
        #   CCC 10% ->  0%: -10%  (sell 100 = 5 CCC, the whole position)
        portfolio = make_portfolio(
            targets={self.aaa: "0.5", self.bbb: "0.5"},
            holdings={self.aaa: "6", self.bbb: "6", self.ccc: "5"},
        )
        full_rebalance = {
            "sells": [(SELL, "AAA", D("1")), (SELL, "CCC", D("5"))],
            "buys": [(BUY, "BBB", D("4"))],
            "cash_after": D("0"),
        }
        nothing = {"sells": [], "buys": [], "cash_after": D("0")}
        cases = {
            "0.01": full_rebalance,
            # A drift exactly at the threshold is traded ("lower than" is skipped).
            "0.10": full_rebalance,
            # Only BBB (+20%) qualifies, but the sells that would fund it are
            # skipped and there is no cash: the buy can't happen.
            "0.15": nothing,
            "0.25": nothing,
        }
        for threshold, expected in cases.items():
            with self.subTest(threshold=threshold):
                plan = portfolio.rebalance(threshold=D(threshold), dry_run=True)
                self.assertCountEqual(trades(plan.sells), expected["sells"])
                self.assertEqual(trades(plan.buys), expected["buys"])
                self.assertEqual(plan.cash_after, expected["cash_after"])

    def test_cash_is_invested(self):
        # Only 1000 cash: buy 500 of each.
        portfolio = make_portfolio(cash="1000", targets={self.aaa: "0.5", self.bbb: "0.5"})
        plan = portfolio.rebalance()
        self.assertEqual(trades(plan.trades), [(BUY, "AAA", D("5")), (BUY, "BBB", D("10"))])
        self.assertEqual(plan.cash_after, D("0"))

    def test_most_underweight_is_bought_first_when_cash_is_short(self):
        # Cash 100 + 45 CCC (900) = 1000.
        #   AAA 0% -> 6.5%:  +6.5% (wants 65)
        #   BBB 0% -> 4%:    +4%   (wants 40)
        #   CCC 90% -> 89.5%: -0.5% (excess 5)
        # Buys need 105 but there are only 100 of cash.
        portfolio = make_portfolio(
            cash="100",
            targets={self.aaa: "0.065", self.bbb: "0.04", self.ccc: "0.895"},
            holdings={self.ccc: "45"},
        )
        with self.subTest("1%: CCC sell skipped, BBB only gets what is left"):
            plan = portfolio.rebalance(threshold=D("0.01"), dry_run=True)
            # AAA (largest gap) is filled first: 65 / 100 = 0.65.
            # BBB gets the remaining 35: 35 / 50 = 0.7 (instead of 0.8).
            self.assertEqual(trades(plan.trades), [(BUY, "AAA", D("0.65")), (BUY, "BBB", D("0.7"))])
            self.assertEqual(plan.cash_after, D("0"))

        with self.subTest("0.5%: CCC sold, so every buy is fully funded"):
            plan = portfolio.rebalance(threshold=D("0.005"), dry_run=True)
            # Sell 5 / 20 = 0.25 CCC -> cash 105 -> 0.65 AAA and 0.8 BBB.
            self.assertEqual(
                trades(plan.trades),
                [(SELL, "CCC", D("0.25")), (BUY, "AAA", D("0.65")), (BUY, "BBB", D("0.8"))],
            )
            self.assertEqual(plan.cash_after, D("0"))

    def test_skipped_sell_caps_the_buy(self):
        # Cash 40 + 5.1 AAA (510) + 5 BBB (250) + 10 CCC (200) = 1000.
        #   AAA 51% -> 50%: -1%  (excess 10)
        #   BBB 25% -> 30%: +5%  (wants 50)
        #   CCC 20% -> 20%:  0%
        portfolio = make_portfolio(
            cash="40",
            targets={self.aaa: "0.5", self.bbb: "0.3", self.ccc: "0.2"},
            holdings={self.aaa: "5.1", self.bbb: "5", self.ccc: "10"},
        )
        with self.subTest(threshold="0.01"):
            # Sell 0.1 AAA (10) -> cash 50 -> buy 1 BBB.
            plan = portfolio.rebalance(threshold=D("0.01"), dry_run=True)
            self.assertEqual(trades(plan.trades), [(SELL, "AAA", D("0.1")), (BUY, "BBB", D("1"))])
            self.assertEqual(plan.cash_after, D("0"))

        with self.subTest(threshold="0.02"):
            # AAA sell skipped -> only 40 cash -> buy 0.8 BBB.
            plan = portfolio.rebalance(threshold=D("0.02"), dry_run=True)
            self.assertEqual(trades(plan.trades), [(BUY, "BBB", D("0.8"))])
            self.assertEqual(plan.cash_after, D("0"))

    def test_indivisible_buy_rounds_down_and_keeps_change(self):
        # 2500 / 1000 = 2.5 DDD -> 2 DDD, 500 stays as cash.
        portfolio = make_portfolio(cash="2500", targets={self.ddd: "1"})
        plan = portfolio.rebalance()
        self.assertEqual(trades(plan.trades), [(BUY, "DDD", D("2"))])
        self.assertEqual(plan.cash_after, D("500"))

    def test_indivisible_sell_rounds_down(self):
        # 3 DDD = 3000, target 50/50 with AAA.
        # Excess DDD is 1500 = 1.5 units -> sell 1 (never more than the excess).
        # Cash 1000 -> AAA wants 1500, gets 1000 = 10 AAA.
        portfolio = make_portfolio(targets={self.ddd: "0.5", self.aaa: "0.5"}, holdings={self.ddd: "3"})
        plan = portfolio.rebalance()
        self.assertEqual(trades(plan.trades), [(SELL, "DDD", D("1")), (BUY, "AAA", D("10"))])
        self.assertEqual(plan.cash_after, D("0"))

    def test_uses_current_market_price(self):
        # Balanced at AAA = 100: 5 AAA (500) + 10 BBB (500).
        # AAA rises to 125: AAA 625, BBB 500, total 1125, target 562.5 each.
        # Sell 62.5 / 125 = 0.5 AAA, buy 62.5 / 50 = 1.25 BBB.
        portfolio = make_portfolio(
            targets={self.aaa: "0.5", self.bbb: "0.5"}, holdings={self.aaa: "5", self.bbb: "10"}
        )
        self.aaa.current_price = D("125")
        self.aaa.save()

        plan = portfolio.rebalance()
        self.assertEqual(plan.total_value, D("1125"))
        self.assertEqual(trades(plan.trades), [(SELL, "AAA", D("0.5")), (BUY, "BBB", D("1.25"))])

    def test_negative_threshold_is_rejected(self):
        with self.assertRaises(ValueError):
            make_portfolio().rebalance(threshold=D("-0.01"))
