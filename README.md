# Do Smart Money Concepts work? A backtest on MES futures

This project tests the Smart Money Concepts (SMC) day-trading model taught
by TJR, the most-followed SMC trader online, and an order-flow model based on
Fabio Valentini's teaching, against two baselines: a daily SMA + RSI strategy
and buy-and-hold. All run on the same Micro E-mini S&P 500 (MES) data with
the same costs.

The SMC strategy follows TJR's sequence step by step: 4h bias → liquidity
sweep → 5m change in order flow → pullback into the FVG/equilibrium → 1m
confirmation entry, stop at the sweep, target the next liquidity. Every rule
and its source is in [`STRATEGY_RULES.md`](STRATEGY_RULES.md); where TJR
leaves a rule open, the choice is marked and was fixed before any backtest on
real data. The order-flow rules are in [`ORDERFLOW_RULES.md`](ORDERFLOW_RULES.md). Python is used instead of TradingView because
TradingView's free plan only loads about 18 trading days of 5-minute bars.

> **Scope of the claim.** These are mechanical versions of discretionary methods.
> The conclusion this project can support is "these rules did X on this data",
> not "SMC does/doesn't work".

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m pytest -q                       # 40 tests, about 3 minutes
python run_backtest.py --synthetic        # dry run on 5 years of random-walk data (--years N to change)
```

Then get real data:

1. Sign up at [databento.com](https://databento.com/signup). New accounts get
   $125 of free historical data credit (credits expire after 6 months).
2. Copy your API key from the portal's API keys page.
3. Download. The script prints the cost and asks before spending anything:

   ```bash
   export DATABENTO_API_KEY=db-xxxxxxxxxxxxxxxx           # Windows: set DATABENTO_API_KEY=...
   python scripts/download_data.py --start 2021-01-01
   ```

   This saves `data/MES_1m.parquet`.

4. Optional, order flow: the order-flow strategy needs every trade with its
   aggressor side (Databento `trades`), which costs far more than 1-minute
   bars. Check the price for a shorter window first; the script asks before
   downloading:

   ```bash
   python scripts/download_orderflow.py --start 2026-04-01 --end 2026-10-01
   ```

   This writes `data/MES_of_1m.parquet` and `data/MES_vap_1m.parquet`. The
   backtest then adds the order-flow strategy automatically, and the report
   gets an **Order-flow window** table comparing all four strategies over
   exactly those dates.

5. Run the backtest. Pick the split date **before** you look at results:

   ```bash
   python run_backtest.py --split 2025-01-01
   ```

Results land in `results/`: `summary.md` (metrics + setup funnel),
`equity.png`, and one `trades_*.csv` per strategy.

### The trade dataset

`trades_orderflow.csv` has the same base columns plus `model`, the balance
(`balance_val`/`vah`/`poc`), the leg (`leg_start_time`, `leg_origin`,
`leg_extreme`), `lvn_price`, `aggression` (big order / stacked imbalance) and
`big_order_threshold`.

`trades_tjr.csv` has one row per trade, times in New York time: entry/exit
time and price, stop, target, exit reason (`target`, `stop`, `flatten` at
16:00), P&L after costs, R multiple, risk in points, max favourable / adverse
excursion (`mfe_pts`, `mae_pts`), and every step of the setup that produced
it: `levels_swept` (e.g. `Asia L+PDL`), `sweep_time`, `sweep_extreme`,
`order_flow` (`BOS`/`IFVG`) and its time, `zone` (`FVG`/`EQ`) and
`zone_price`, `entry_confirmation` (`BOS`/`IFVG`), `signal_time`,
`target_level` and `bias_4h`. That's enough to break results down by level,
time of day or confirmation type, as long as those breakdowns are reported as
exploration, not as new rules.

## The strategies

| | Entries | Stop | Target |
|---|---|---|---|
| **SMC: TJR model** | 1-minute bars. With the 4h bias, a previous-day / Asia / London / hourly low is swept; a 5m candle closes back through the latest swing or an inverse FVG; price pulls back into the new 5m FVG (or the move's 50%); a 1m break of structure or inverse FVG out of it triggers a market entry, 08:00–11:00 ET. | Sweep extreme | Nearest untaken liquidity ≥ 1R away |
| **Order flow: Fabio model** | 1-minute order-flow bars. Balance = previous day's value area. Trend: a 5m close outside value, then a pullback into a low-volume node (LVN) of the impulse leg's volume profile. Mean reversion: a failed push outside value, a 5m close back inside, then a pullback into an LVN of the reclaim leg. Both need aggression at the LVN (a big aggressive order or 3+ stacked footprint imbalances); market entry, 08:00–11:00 ET. | 2 ticks beyond the aggression bar / LVN | Previous balance POC (trend) or balance POC (mean reversion), ≥ 1R |
| **SMA + RSI (daily)** | Daily bars. Long when SMA(20) > SMA(50) and RSI(9) < 30; short when SMA(20) < SMA(50) and RSI(9) > 70. Stop-and-reverse: held until the opposite signal. Sized at 100% of equity. Pine original: `pine/sma_rsi_daily.pine`. | none | none |
| **Buy & hold** | Buy at the start, hold to the end, roll each quarter. Sized so notional ≈ starting capital (no leverage). | none | none |

The SMC strategy day trades: one position at a time, 0.5% of equity risked
per trade (capped at 10 contracts), and anything still open is closed at
16:00 ET. There is no daily trade cap beyond that. It runs on 1-minute bars
because TJR's entry is a 1-minute confirmation; its 5-minute, hourly and
4-hour bars are built from completed 1-minute bars, so it only ever sees what
a trader watching live would see.

SMA + RSI is a different kind of strategy: it works on daily bars and holds
positions for weeks, overnight and through rolls. Its signal is read at the
daily close (the 5-minute bar ending 17:00 ET) and fills at the next session's
open (18:00 ET). A position held through a quarterly roll pays a round trip of
costs, the same as buy & hold. That makes it closer to buy & hold than to SMC
in exposure (100% time in market once the first signal fires), so compare it
with both on Sharpe and drawdown, not on trade statistics.

Change any setting without editing code:

```bash
python run_backtest.py --set tjr.use_bias=false           # without the 4h bias
python run_backtest.py --set tjr.min_target_rr=1.5
python run_backtest.py --set sma.allow_shorts=false       # long-only SMA + RSI
```

The prefix picks the strategy: `tjr.` (`TJRConfig`,
`backtest/strategies/smc.py`), `of.` (`FabioConfig`,
`backtest/strategies/orderflow.py`) and `sma.` (`SMARSIConfig`,
`backtest/strategies/sma_rsi.py`). Every variation you run
counts as a test. Write each one down and report it, including the ones you
drop.

## How the engine works

`backtest/engine.py` steps through bars (1-minute for SMC, 5-minute for the others) the way a TradingView strategy
does (`calc_on_every_tick=false`, `process_orders_on_close=false`):

- The strategy decides on each bar's **close**. Market orders fill at the **next
  bar's open** plus 1 tick of slippage.
- Limit entries rest until filled or cancelled. Stops and targets are checked
  inside every bar.
- Costs: $1 per contract per side, 1 tick slippage on market and stop fills
  (same as the Pine script). Buy & hold also pays a round trip at every roll.
- **Intrabar ambiguity.** When one bar touches both the stop and the target, the
  default (`--intrabar conservative`) assumes the stop came first. That can only
  make results look worse, never better. `--intrabar tv` uses TradingView's
  rule instead (open closer to the high ⇒ high first).

## Guarding against lookahead bias

Lookahead bias, where a decision accidentally uses data from the future, is
the most common way backtests lie. `tests/test_no_lookahead.py` checks for it
two ways:

1. **Decision replay.** The strategy runs on the full data and again on data cut
   off right after each of its own decisions and state changes (sweep, 5m
   shift, pullback, invalidation, entry). Every decision, stop and zone level
   must be identical.
2. **Prefix-stable features.** Every precomputed series (previous-day levels,
   4h bias, hourly swings, 1m swings, session masks, daily SMA/RSI) is
   recomputed on truncated copies of the data and must match the full run bar
   for bar. A separate test checks that no 5-minute bar is used before its
   last minute has closed.

The tests were checked by planting leaks and confirming each makes the suite
fail. In the SMC strategy: today's range used as "previous day", higher-timeframe
bars used one minute early, 4h/1h values known 30 minutes early, 1m swings known
at the swing bar, the stop seeing the next bars' low, and the equilibrium seeing
the next bars' high. In SMA + RSI: each day's indicators computed from the next
day's close. The first version of the replay test missed the last two; cutting
after every state change fixed that.

The order-flow strategy has the same two checks
(`tests/test_orderflow_lookahead.py`), mutation-tested with five planted
leaks: today's value area used as "previous day", the big-order threshold
including today, the leg's volume profile seeing the next minutes (twice), and
aggression read from the next bar. `tests/test_orderflow_strategy.py` replays
a hand-built trade tape where every step was worked out by hand (value area,
reclaim, the exact LVN, the stacked imbalances, entry, stop, target), and
`tests/test_orderflow_data.py` checks the trade aggregation with exact
answers.

`tests/test_smc.py` also runs the SMC strategy on a hand-built 1-minute price
path where every step was worked out by hand (sweep time, 5m BOS bar,
equilibrium level, 1m inverse-FVG entry, fill price, target level), plus
variants for the 4h bias, invalidation, the entry window and the 1R target
minimum.

## Reading the results honestly

- **Train vs test.** Tune settings only on the train period. The test period
  is the result you report. If you tune after seeing test results, it becomes
  a second train set.
- **Avg R and its 95% CI.** R is profit divided by the amount risked. The
  confidence interval comes from bootstrapping the trades. If it straddles 0,
  the sample can't tell an edge from luck.
- **`p (edge <= 0)`.** The share of bootstrap resamples with average R ≤ 0
  (average $ per trade for SMA + RSI, which has no stop and so no R). Below
  0.05 is the usual bar, and it only means anything if you didn't try dozens
  of settings to get there. With fewer than 20 trades no CI or p is reported:
  on 5 years of *random* data the SMA + RSI test year had 8 trades, Sharpe
  1.86 and a bootstrap p of 0.02, which is pure luck. Over the full 5 years
  it was Sharpe 0.11.
- **SMA + RSI trades rarely.** Expect a few trades a year, because it only
  reverses when the 20/50 trend flips and RSI reaches an extreme. Judge it
  on the full period's daily Sharpe and drawdown, not on a one-year test.
- **Compare risk-adjusted numbers.** Buy & hold is in the market 100% of the
  time; the SMC strategy is in it a few percent. Compare Sharpe and max
  drawdown, not just net profit.
- **The setup funnel** shows where SMC setups drop out (outside the window,
  no pullback, invalidated, outside the window, against the 4h bias, no
  target). Use it to explain the trade count.

## Differences from the TradingView version

| | TradingView `MES1!` | This project |
|---|---|---|
| Roll handling | unadjusted prices; level jumps at each roll | back-adjusted by default (`--no-adjust` to match TV) |
| Roll date | TradingView's own rule | Databento volume roll (`MES.v.0`) |
| Same-bar stop/target | TV heuristic | conservative by default (`--intrabar tv` to match) |
| Positions held across a roll | unadjusted MES1! books the roll gap as P&L | back-adjusted, round-trip cost charged |
| Swing ties | TV's internal rule | a swing must be strictly higher/lower than its neighbours |

Only SMA + RSI has a TradingView version (`pine/sma_rsi_daily.pine`). To check
the port, add the Pine script to a daily MES1! chart (daily history goes back
years even on the free plan) and compare its List of Trades with
`results/trades_sma_rsi.csv` from an unadjusted run:

```bash
python run_backtest.py --no-adjust --intrabar tv
```

Signal dates should match. Fill prices can differ by vendor data and by the
roll date. A different signal date means a bug in one of the two versions.
The SMC strategy exists only in Python; `tests/test_smc.py` is its check.

## Project layout

```
backtest/
  data.py          load, back-adjust, resample, trading-day labels
  engine.py        broker / fill simulation
  indicators.py    ATR, EMA, RSI, pivots (TradingView definitions)
  metrics.py       Sharpe, drawdown, R stats, bootstrap
  synthetic.py     random-walk test data
  strategies/      smc.py, orderflow.py, sma_rsi.py, buy_hold.py
scripts/download_data.py
run_backtest.py
tests/
STRATEGY_RULES.md  every SMC rule with its source
ORDERFLOW_RULES.md every order-flow rule with its source
backtest/orderflow.py        trades -> order-flow bars, volume profile, value area, LVNs
scripts/download_orderflow.py  trade-level download, condensed week by week
pine/              SMA + RSI Pine original; archive/ holds the earlier SMC Pine experiments
```

## Limitations

- **Order flow on random data.** The synthetic test data has random wicks,
  which hit tight stops far more often than wide targets. The order-flow
  strategy uses 1–3 point stops, so it looks especially bad on synthetic data
  (about −0.4R to −0.8R per trade); without wicks it's near zero. Only real
  data says anything about it.
- **Aggressive order size** is approximated by summing trade records with the
  same timestamp and side.
- 1-minute bars are the finest data. There is no tick data, so the order of
  prices inside a bar is an assumption (see above).
- No news filter by default. Add dates with `--set tjr.news_dates=2025-01-10,2025-02-07`.
- Overnight risk is not modelled for the SMC strategy, since it is always
  flat by 16:00 ET.
- Commission of $1/side is a rough estimate. Check your broker's all-in cost
  (exchange and clearing fees included) and set it in `EngineConfig`.
