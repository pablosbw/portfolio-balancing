"""
Executing a rebalance: orders are created and the portfolio's cash and holdings are updated.
"""

from decimal import ROUND_DOWN, Decimal

from django.test import TestCase

from portfolio.models import DUMMY_USER_ID, Order, Portfolio

from .helpers import make_portfolio, make_stock

D = Decimal


def holdings(portfolio: Portfolio) -> dict[str, tuple[Decimal, Decimal]]:
    """{asset_id: (quantity, average price)} of the saved portfolio."""
    portfolio.refresh_from_db()
    return {e["assetId"]: (D(e["quantity"]), D(e["price"])) for e in portfolio.current_composition}


def placed_orders(portfolio: Portfolio) -> list[tuple[str, str, Decimal]]:
    return sorted((o.side, o.asset.symbol, o.executed_quantity) for o in portfolio.orders.all())


class RebalanceExecutionTests(TestCase):
    def test_seed_example_creates_two_sells_and_one_buy(self):
        # Same data as the seed: target 40% META / 60% AAPL, 1000 cash,
        # holding 15 AAPL, 8 META and 4 MSFT (MSFT isn't in the target).
        aapl = make_stock("AAPL", "230.5")
        meta = make_stock("META", "720.25")
        msft = make_stock("MSFT", "510.75")
        portfolio = make_portfolio(
            cash="1000",
            targets={meta: "0.4", aapl: "0.6"},
            holdings={aapl: "15", meta: "8", msft: "4"},
        )
        value_before = portfolio.total_value()

        plan = portfolio.rebalance()

        # One FILLED order per trade, executed exactly as proposed.
        self.assertEqual(placed_orders(portfolio), [
            ("BUY", "AAPL", D("16.91973969")),
            ("SELL", "META", D("1.18986463")),
            ("SELL", "MSFT", D("4")),
        ])
        for order in portfolio.orders.all():
            self.assertEqual(order.status, Order.Status.FILLED)
            self.assertEqual(order.user_id, DUMMY_USER_ID)
            self.assertEqual(order.executed_quantity, order.proposed_quantity)
            self.assertEqual(order.executed_price, order.proposed_price)
            self.assertEqual(order.proposed_price, order.asset.current_price)

        # Holdings: META reduced, AAPL increased, MSFT gone.
        quantities = {asset_id: qty for asset_id, (qty, _) in holdings(portfolio).items()}
        self.assertEqual(quantities, {
            str(aapl.pk): D("15") + D("16.91973969"),
            str(meta.pk): D("8") - D("1.18986463"),
        })

        # All the cash is spent, except for dust smaller than one cent.
        self.assertEqual(portfolio.cash, plan.cash_after.quantize(D("0.00000001"), rounding=ROUND_DOWN))
        self.assertTrue(D("0") <= portfolio.cash < D("0.01"), portfolio.cash)
        # Trading at market price moves value between cash and stocks, it doesn't create or destroy it.
        self.assertAlmostEqual(portfolio.total_value(), value_before, places=2)

        # The portfolio is now balanced: rebalancing again places no more orders.
        self.assertEqual(portfolio.rebalance().trades, [])
        self.assertEqual(portfolio.orders.count(), 3)

    def test_sell_keeps_average_cost_and_new_position_starts_at_buy_price(self):
        # 10 AAA bought at 1 (see helper), now worth 100 each. Target 50/50 with BBB.
        # Sell 5 AAA -> 500 cash -> buy 10 BBB @ 50.
        aaa = make_stock("AAA", "100")
        bbb = make_stock("BBB", "50")
        portfolio = make_portfolio(targets={aaa: "0.5", bbb: "0.5"}, holdings={aaa: "10"})

        portfolio.rebalance()

        self.assertEqual(placed_orders(portfolio), [("BUY", "BBB", D("10")), ("SELL", "AAA", D("5"))])
        self.assertEqual(holdings(portfolio), {
            str(aaa.pk): (D("5"), D("1")),
            str(bbb.pk): (D("10"), D("50")),
        })
        self.assertEqual(portfolio.cash, D("0"))

    def test_buy_blends_into_average_cost(self):
        # 10 AAA at 1 + 10 AAA at 100 -> 20 AAA at (10 + 1000) / 20 = 50.5.
        aaa = make_stock("AAA", "100")
        portfolio = make_portfolio(cash="1000", targets={aaa: "1"}, holdings={aaa: "10"})

        portfolio.rebalance()

        self.assertEqual(holdings(portfolio), {str(aaa.pk): (D("20"), D("50.5"))})
        self.assertEqual(portfolio.cash, D("0"))

    def test_dry_run_places_no_orders_and_changes_nothing(self):
        aaa = make_stock("AAA", "100")
        portfolio = make_portfolio(cash="1000", targets={aaa: "1"})
        before = (portfolio.cash, portfolio.current_composition)

        plan = portfolio.rebalance(dry_run=True)

        self.assertEqual(len(plan.trades), 1)
        self.assertEqual(portfolio.orders.count(), 0)
        portfolio.refresh_from_db()
        self.assertEqual((portfolio.cash, portfolio.current_composition), before)

    def test_drift_below_threshold_places_no_orders(self):
        # 5 AAA (500) + 10 BBB (500) target 0.505 / 0.495: 0.5% drift, below 1%.
        aaa = make_stock("AAA", "100")
        bbb = make_stock("BBB", "50")
        portfolio = make_portfolio(
            targets={aaa: "0.505", bbb: "0.495"}, holdings={aaa: "5", bbb: "10"}
        )
        portfolio.rebalance(threshold=D("0.01"))
        self.assertEqual(portfolio.orders.count(), 0)
