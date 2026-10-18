# SMC strategy: TJR's model, and where each rule comes from

The SMC side of this project is one strategy: the sequence TJR teaches,
coded step by step. TJR says he applies his confluences with discretion and
doesn't give a rigid rulebook, so each step below is either taken from his
own description or marked **(choice)** where a precise rule had to be picked.
Every choice was fixed before any backtest on real data and is not to be tuned
to results.

Main sources: TJR's video *Path to Profitability: TJR's Strategy Explained*
([transcript](https://sozai.app/transcript/path-to-profitability-tjr-strategy/),
[summary](https://youtubesummary.com/summary/TEp3a-7GUds)). The quoted lines
are from the transcript's summary of his words.

Longs are described; shorts are the exact mirror.

## The sequence

| Step | Rule in the code | Source / choice |
|---|---|---|
| **1. Bias** | Trade only in the direction of 4-hour market structure: bullish after a 4h candle closes above the latest 4h swing high, bearish after one closes below the latest 4h swing low. | Bias from the higher-timeframe trend, "e.g., 4-hour bullish/bearish market structure" (summary). The close-through-swing definition is a **(choice)**. 4h candles start 18:00, 22:00, 02:00 … ET like TradingView's CME 4h chart. |
| **2. Liquidity** | Previous day high/low, Asia high/low (20:00–00:00 ET), London high/low (02:00–05:00 ET), and the latest hourly swing high/low. | TJR marks "previous day high and low, Asia session high and low, London session high and low, hourly highs and lows". Session hours are a **(choice)** (the common ICT/TJR ranges). |
| **3. Sweep** | Price trades through one of those levels for the first time since it formed. A swept low arms a long setup. | "pushing above a high, a significant high, or below a significant low". A close back inside isn't required; step 4 is the reversal proof. |
| **4. Change in order flow** | A 5-minute candle closes above the latest 5m swing high (break of structure), or closes above a bearish 5m FVG that formed after that swing high (inverse FVG). | "a break of structure or an inverse fair value gap", on the 5-minute in his example. |
| **5. Pullback** | Wait for price to come back into the bullish 5m FVG left by the new up-move (its top edge); if the move left no FVG, wait for the move's equilibrium (50% between the sweep low and the high since). | "look for continuation … price returning to equilibrium or a fair value gap". FVG-first, else EQ, is a **(choice)**. |
| **6. Entry** | After the pullback, a 1-minute candle closes above a 1m swing high or a bearish 1m FVG that formed during the pullback → buy at the next 1-minute open. | "wait for price to fill the confluence that you want and then wait for confirmation down and out of it"; he drops "to the 1-minute for the entry" and calls entering on the touch risky. |
| **7. Stop** | At the sweep low (the lowest price since the sweep). | "I put my stop loss above these highs" (his short example). |
| **8. Target** | The nearest untaken liquidity level above entry (previous day high, Asia/London high, hourly swing high) that is at least 1R away. No trade if there is none. | "I targeted all of my draws and liquidity". He scales out at several pools; one target keeps it mechanical **(choice)**. The 1R minimum is a **(choice)**: without it the nearest level can be a few ticks away, a trade he wouldn't take. |

## Other rules

| Rule | Value | Source / choice |
|---|---|---|
| Swing | a high with 2 lower highs on each side (any timeframe), confirmed 2 candles later | **(choice)**, standard 5-candle fractal |
| Entry window | the 1m confirmation must be between 08:00 and 11:00 ET (fill before 11:00) | project setting (the NY morning session TJR trades) |
| Setup life | ends at the CME day boundary (18:00 ET), when it's used for an entry decision (even if blocked), or when price makes a new low after step 4 (the sweep low would be the stop) | **(choice)** |
| Positions | one at a time, no daily cap; closed at 16:00 ET if still open | TJR gives no daily cap; no overnight holds is a **(choice)** for a day-trading test |
| Risk | 0.5% of equity per trade, 1–10 MES contracts | project setting |
| Timeframes | 4h bias, 1h liquidity, 5m order flow, 1m entry, all built from completed 1-minute bars | as above |

## Left out on purpose

- **SMT divergence.** TJR uses it as a sign a sweep is likely, not a required
  step, and it needs a second market's data.
- **Discretionary higher-timeframe levels and imbalances** beyond the ones
  listed, and **scaling out** at several targets.
- **News filter.** Available with `--set tjr.news_dates=2025-01-10,...`, off by default.

## What this is not

It's a mechanical version of a discretionary method. A result here means
"these rules did X on MES from START to END". It does not show whether TJR, or
anyone trading his ideas with judgment, makes money.
