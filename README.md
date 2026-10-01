# Portfolio Balancing

Portfolio management module for a personal investments and trading app.
It's built with Django and PostgreSQL and runs in Docker.

## Run

```bash
docker compose up --build
```

On start, the `web` service runs the migrations, seeds the database if it is empty, and starts the dev server at http://localhost:8000.
Restarting keeps your data. To reset the sample data (prices, cash, holdings, and the orders a rebalance created), run:

```bash
docker compose exec web python manage.py seed
```

To use the Django admin, run `docker compose exec web python manage.py createsuperuser` and open http://localhost:8000/admin.

## Data model

All prices and quantities are `NUMERIC(16, 8)`: up to 16 digits in total, 8 of them after the decimal point. In Python they're handled as `Decimal`, never `float`.
Primary keys are UUIDs.
There's no authentication yet, so every row belongs to the user `"dummy"`.

| Table | Fields |
|---|---|
| `Stock` | `asset_id`, `symbol`, `market`, `company_name`, `currency` (USD), `is_divisible`, `current_price`. `get_current_price()` returns the last available price. No price history is kept. |
| `Order` | `order_id`, `user_id`, `portfolio_id` (FK to Portfolio, nullable), `asset_id` (FK to Stock), `side` (BUY/SELL), `status` (PENDING/PARTIALLY_FILLED/CANCELED/FILLED), `proposed_quantity`, `proposed_price`, `executed_quantity`, `executed_price`, `created_at` |
| `Portfolio` | `portfolio_id`, `user_id`, `currency`, `cash`, `expected_composition` (`[{assetId, weight}]`), `current_composition` (`[{assetId, quantity, price}]`) |

The two compositions are stored as JSON, with numbers kept as strings so they don't lose precision.
`Portfolio.save()` validates them before writing:
- weights are `>= 0` and add up to exactly 1
- quantities and prices are `>= 0`
- no `assetId` appears twice
- every `assetId` exists in `Stock`

In `current_composition`, `price` is the average price paid for the position.
Market value is computed with the stock's current price instead.

## Seed data

- **5 stocks:** AAPL, META, MSFT, GOOGL (NASDAQ) and BRK.A (NYSE, not divisible). The prices are sample values, not live market data.
- **6 orders**, covering every status. The portfolio's holdings are computed from the executed quantities of these orders, so the two always agree.
- **1 portfolio:**
  - Cash: 1,000 USD.
  - Target: 40% META / 60% AAPL.
  - Holdings: 15 AAPL, 8 META and 4 MSFT. MSFT isn't in the target, so it should be sold when rebalancing.

## Rebalance

```bash
docker compose exec web python manage.py rebalance                    # threshold 1%
docker compose exec web python manage.py rebalance --threshold 0.05
docker compose exec web python manage.py rebalance --dry-run          # only show the plan
docker compose exec web python manage.py orders                       # orders placed and current holdings
docker compose exec web python manage.py orders --status FILLED
```

### Main idea

The rebalance works with amounts of money, not percentages.

- **One pool of money.** The portfolio's value is its cash plus each holding valued at today's price.
- **Targets become amounts.** Each target weight says how much of that pool each stock should hold.
- **Each difference is a trade.** If a stock holds less than its target amount, buy the difference. If it holds more, sell it. A stock that isn't in the target has a target of 0, so all of it is sold.
- **Sell first, then buy.** Sells produce the cash the buys need. Together with the cash already in the portfolio, that pays for the buys.
- **Skip small differences.** A threshold (1% of total value by default) ignores differences too small to be worth a trade.
- **Never overspend.** Skipping small sells can leave less cash than the buys need. In that case the largest differences are bought first, and a buy is reduced to the cash left.

Example with the seeded portfolio, whose total value is 12,262.50:

| Stock | Current value | Target | Target value | Difference | Trade |
|---|---:|---:|---:|---:|---|
| AAPL | 3,457.50 (28.2%) | 60% | 7,357.50 | +3,900.00 | buy 16.92 AAPL |
| META | 5,762.00 (47.0%) | 40% | 4,905.00 | −857.00 | sell 1.19 META |
| MSFT | 2,043.00 (16.7%) | 0% | 0.00 | −2,043.00 | sell 4 MSFT |
| Cash | 1,000.00 | | | | |

The sells bring in 857 + 2,043 = 2,900. With the 1,000 of cash, that's 3,900, exactly what the AAPL buy needs. After the rebalance, cash is 0 and the portfolio is 60% AAPL / 40% META.

### Algorithm

The algorithm is in [portfolio/rebalance.py](portfolio/rebalance.py) and has no database access:

1. **Total value** = cash + Σ quantity × *current* price.
2. **Drift** per asset = target weight − current weight. An asset that is held but not in the target has weight 0, so it is sold entirely.
3. Drifts **below the threshold** are ignored. A drift exactly equal to the threshold is traded.
4. **Sells** remove each overweight asset's excess. The proceeds are added to the cash.
5. **Buys** go in order, largest drift first.
6. Before each buy, the remaining cash is checked. Skipped sells can leave less cash than the buys need, so a buy is reduced or skipped.

Quantities are always rounded **down**: to 8 decimals, or to whole units for indivisible assets such as BRK.A.
This way the plan never sells more than the excess and never spends cash it doesn't have.

### Execution

`Portfolio.rebalance(threshold=Decimal("0.01"), dry_run=False)` computes the plan and executes it in one transaction:
- one `Order` per trade, linked to the portfolio. There is no broker yet, so orders are created `FILLED`, with executed quantity and price equal to the proposed ones (the current market price).
- `current_composition` is updated. A buy blends its price into the position's average cost; a sell reduces the quantity and keeps the average cost; positions sold entirely are removed.
- `cash` is updated, rounded down to 8 decimals.

The portfolio row is locked during the rebalance, so two runs at the same time can't spend the same cash.
With `dry_run=True`, it returns the plan and changes nothing.

## Simulating price changes

```bash
docker compose exec web python manage.py update_prices                 # every stock moves randomly by ±1%
docker compose exec web python manage.py update_prices BRK.A,0.9       # only BRK.A, -10%
docker compose exec web python manage.py update_prices BRK.A,0.9 AAPL  # BRK.A -10%, AAPL random, the rest unchanged
docker compose exec web python manage.py update_prices --seed 42       # reproducible random moves
```

`python manage.py seed` puts back the seeded prices.

## Tests

```bash
docker compose exec web python manage.py test portfolio
```

The tests cover:
- weight validation: sum below 1, sum above 1, negative weights
- the rebalance with several thresholds, including a drift exactly at the threshold
- buy order when cash is short
- indivisible assets
- current market price vs. acquisition price
- executing the rebalance: the seed example creates 2 sells and 1 buy, all FILLED; holdings, average cost and cash are updated; a second rebalance places no orders; a dry run changes nothing
- the `update_prices` command
