# Order-flow strategy: Fabio Valentini's model, and where each rule comes from

The order-flow side of this project follows Fabio Valentini, the most-followed
order-flow futures trader online (auction market theory, volume profile and
aggressive order flow on NQ/ES). As with the SMC model, each rule is either
taken from a description of his method or marked **(choice)** where the
sources give no number. Every choice was fixed before any backtest on real
data and is not to be tuned to results.

Main sources: [Tradezella, "Auction Market Strategy" (Fabio Valentini)](https://www.tradezella.com/strategies/auction-market-strategy)
and [Tradezella, "Auction Market Playbook"](https://www.tradezella.com/playbooks/auction-market-playbook)
(third-party write-ups of his teaching; quotes below are from them), plus
[NinjaTrader, "Order Flow Trading for Prop Traders"](https://prop.ninjatrader.com/blogs/order-flow-trading-for-prop-traders/)
for standard definitions.

Core rule from the source: market state, location and aggression must all
line up. "If even one is missing, you stay flat."

## Data

| Item | Rule in the code | Source / choice |
|---|---|---|
| Trades | Databento `trades` (CME Globex), every trade with its aggressor side: B = buyer lifted the offer, A = seller hit the bid | Databento schema docs |
| Aggressive order | trade records with the same timestamp, side and contract are one order | **(choice)**, an approximation: one aggressive order can fill many resting orders |
| Bars | 1-minute bars with buy volume, sell volume (delta = buy − sell), the largest aggressive buy and sell order, and stacked footprint imbalances | built from the trades |
| Volume profile | volume at every price, per minute | built from the trades |

## Shared definitions

| Item | Rule in the code | Source / choice |
|---|---|---|
| Balance | the previous trading day's volume profile: POC (most traded price), value area VAL–VAH holding 70% of volume | "use the previous day's profile as the balance reference"; value area = "the range containing approximately 70% of that volume" (NinjaTrader) |
| Value area method | grow one tick at a time from the POC toward the side with more volume (ties go up) | **(choice)**, common software method |
| Low-volume node (LVN) | on the leg's own profile, smoothed over 3 ticks: a local minimum at or below 30% of the leg's peak, not in the outer 10% of the leg | "Identify Low-Volume Nodes (LVNs) inside that move"; the numbers are **(choice)** |
| Aggression | a 1-minute bar with **either** a big aggressive order in the trade direction (and delta on the same side) **or** 3+ stacked footprint imbalances in that direction | "Big buy bubbles or imbalance for longs"; "No aggression = no trade" |
| Big order | the bar's largest aggressive order ≥ the 99th percentile of each minute's largest order, median over the previous 20 trading days | **(choice)**: the source doesn't say what counts as big; a relative threshold adapts as volume changes |
| Footprint imbalance | buyers at a price ≥ 3× sellers one tick lower (diagonal), with ≥ 5 contracts; stacked = 3+ consecutive prices | **(choice)**: 300% and 3 stacked are common defaults in footprint software |
| At the LVN | the aggression bar trades within 1 tick of the node and closes at it or on the trade's side (at most 1 tick through) | **(choice)**: a bar that falls through the node isn't "aggression at the LVN" |
| Stop | 2 ticks beyond the aggression bar's extreme or the LVN, whichever is further | "Place just beyond the aggressive print … a 1–2 tick buffer" |
| Target | at least 1R away, else no trade | "Target the … POC"; the 1R minimum is a **(choice)** (same as the SMC model); the source cites 1:2.5–1:5 as typical |

## Trend model (out of balance → seek new balance)

Longs described; shorts are the mirror image.

| Step | Rule in the code | Source / choice |
|---|---|---|
| State | a 5-minute candle closes above the previous day's VAH (crossing from at/below it) | "displacement and momentum away from the prior value"; the close-above-VAH test is a **(choice)** |
| Impulse leg | from the latest 5-minute swing low (2 bars each side) before that close, or the day's start if it's older, to the leg's high (extended while price makes new highs) | "Take the impulse leg that broke the structure … from the start of the impulse to the top of the move"; swing definition **(choice)** |
| Location | a pullback (after the leg's latest high) into an LVN of the leg's profile | "Price returns to the LVN" |
| Entry | aggression at the LVN → market order at the next 1-minute open | "Enter only when aggression shows up" |
| Target | the nearest previous balance POC above (POCs of the last 20 trading days) | "Target the previous balance POC"; 20 days is a **(choice)** |
| Invalidated | price trades below the leg's starting low | **(choice)** |

## Mean-reversion model (failed breakout → back to balance)

| Step | Rule in the code | Source / choice |
|---|---|---|
| State | price trades below the previous day's VAL ("push out of balance"), then fails: a 5-minute candle closes back above VAL ("a clear reclaim inside balance") | "Watch for the price to push out of balance and then fail" |
| Reclaim leg | from the lowest price made outside value to the leg's high | "Apply volume profile on the reclaim leg and mark the LVNs" |
| Location / entry | pullback into an LVN of the reclaim leg + aggression → market order at the next open | "Do not take the first move back … A pullback into the reclaim leg" |
| Target | the balance POC (previous day's POC) | "Target the balance POC (center of value)" |
| Invalidated | price trades below the leg's starting low | **(choice)** |

## Other rules

| Rule | Value | Source / choice |
|---|---|---|
| Entry window | 08:00–11:00 ET for both models | project setting. The source says the trend model "works best in the New York session" and the mean-reversion model in London or quiet conditions; one window keeps the comparison with SMC fair **(choice)** |
| Setup life | ends at the CME day boundary, on invalidation, or when it's used for an entry decision | **(choice)** |
| Positions | one at a time; closed at 16:00 ET if still open | **(choice)** |
| Risk | 0.5% of equity per trade, 1–10 contracts | project setting; the source uses "0.25% to 0.5%" |

## Left out on purpose

- **Moving the stop to break-even on strong CVD.** The source gives no
  measurable definition of "strong pressure".
- **Discretion about market state** ("If price is just rotating, skip it")
  beyond the mechanical tests above.
- **The order book (DOM) and spoofing/iceberg reading.** That needs full
  order-book data, which is much larger than trades.

## What this is not

A mechanical version of a discretionary scalping method. Fabio reads the tape
live; this code can't. A result here means "these rules did X on MES over
these dates".
