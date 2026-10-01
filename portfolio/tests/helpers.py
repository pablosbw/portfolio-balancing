from decimal import Decimal

from portfolio.models import Portfolio, Stock


def make_stock(symbol: str, price: str, is_divisible: bool = True) -> Stock:
    return Stock.objects.create(
        symbol=symbol, market="TEST", company_name=f"{symbol} Corp", currency="USD",
        is_divisible=is_divisible, current_price=Decimal(price),
    )


def make_portfolio(cash: str = "0", targets: dict | None = None, holdings: dict | None = None) -> Portfolio:
    """``targets``: {stock: weight}. ``holdings``: {stock: quantity}."""
    return Portfolio.objects.create(
        cash=Decimal(cash),
        expected_composition=[
            {"assetId": str(stock.pk), "weight": weight} for stock, weight in (targets or {}).items()
        ],
        current_composition=[
            # Acquisition price deliberately differs from the market price:
            # the rebalance must value holdings at the *current* price.
            {"assetId": str(stock.pk), "quantity": quantity, "price": "1"}
            for stock, quantity in (holdings or {}).items()
        ],
    )
