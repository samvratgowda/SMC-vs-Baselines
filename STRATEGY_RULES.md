# SMC (ICT model): the rules, and where each comes from

The SMC side of this project is one strategy: a Smart Money Concepts model
built from the Inner Circle Trader's (ICT) concepts, coded step by step: a
higher-timeframe bias, a sweep of liquidity, a shift in market structure, a
pullback into the fair value gap, and an entry with the stop beyond the sweep
and the target at the opposing liquidity. ICT's method is discretionary and
doesn't come as a rigid rulebook, so each step below either follows a
published description of his concepts or is marked **(choice)** where a
precise rule had to be picked. Every choice was fixed before any backtest on
real data and is not to be tuned to results.

Main sources (third-party write-ups of ICT's teaching):
[LuxAlgo, "Model 2022"](https://www.luxalgo.com/library/concept/model-2022/),
[LuxAlgo, "ICT Inversion Fair Value Gap"](https://www.luxalgo.com/library/indicator/PwW7JLzH-ict-inversion-fair-value-gap-ifvg/)
and [the "ICT Liquidity Sweep Asia/London" script on TradingView](https://www.tradingview.com/script/6fqUkyaO-ICT-Liquidity-Sweep-Asia-London-1-Trade-per-High-Low).
Text in quotation marks is quoted from these pages; everything else is paraphrased from them.

Longs are described; shorts are the exact mirror.

## The sequence

| Step | Rule in the code | Source / choice |
|---|---|---|
| **1. Bias** | Trade only in the direction of 4-hour market structure: bullish after a 4h candle closes above the latest 4h swing high, bearish after one closes below the latest 4h swing low. | Model 2022 sets bias and the target pool on a higher timeframe (roughly the 15-minute to hourly chart). Using the 4-hour chart and the close-through-swing definition are **(choice)**. 4h candles start 18:00, 22:00, 02:00 … ET like TradingView's CME 4h chart. |
| **2. Liquidity** | Previous day high/low, Asia high/low (20:00–00:00 ET), London high/low (02:00–05:00 ET), and the latest hourly swing high/low. | Model 2022's resting pools: the prior-day high and low, session extremes, and equal highs or lows. Asia 20:00–00:00 and London 02:00–05:00 ET follow the ICT Asia/London sweep script. Hourly swing highs/lows are a **(choice)** for resting liquidity at recent swings. |
| **3. Sweep** | Price trades through one of those levels for the first time since it formed. A swept low arms a long setup. | Model 2022: longs follow a sweep of an old low, shorts a sweep of an old high. A close back inside isn't required; step 4 is the reversal proof. |
| **4. Shift in structure** | A 5-minute candle closes above the latest 5m swing high (break of structure / market structure shift), or closes above a bearish 5m FVG that formed after that swing high (inverse FVG). | Model 2022: the shift happens when the reversal leg closes through the most recent opposing swing point on the execution timeframe (1–5 minute charts). Inverse FVG: a fair value gap that price closes through flips to the other side ("Inversion Fair Value Gaps are a core concept in ICT (Inner Circle Trader) methodologies"). The 5-minute timeframe is a **(choice)** within that range. |
| **5. Pullback** | Wait for price to come back into the bullish 5m FVG left by the new up-move (its top edge); if the move left no FVG, wait for the move's equilibrium (50% between the sweep low and the high since). | Model 2022: entry on a retracement into the gap the displacement leg leaves behind. The equilibrium fallback is a **(choice)** (Model 2022 notes that some traders use a retracement band in place of the gap). |
| **6. Entry** | After the pullback, a 1-minute candle closes above a 1m swing high or a bearish 1m FVG that formed during the pullback → buy at the next 1-minute open. | **(choice)**: Model 2022 allows a resting limit in the gap or acting on its first touch; this project waits for a 1-minute confirmation out of the gap instead of entering blind at the touch. |
| **7. Stop** | At the sweep low (the lowest price since the sweep). | Model 2022: stop past the swept high or low (the raided extreme). |
| **8. Target** | The nearest untaken liquidity level above entry (previous day high, Asia/London high, hourly swing high) that is at least 1R away. No trade if there is none. | Model 2022: target the opposing liquidity pool. One target, and the 1R minimum, are **(choice)**: without a minimum the nearest level can be a few ticks away. |

## Other rules

| Rule | Value | Source / choice |
|---|---|---|
| Swing | a high with 2 lower highs on each side (any timeframe), confirmed 2 candles later | **(choice)**, standard 5-candle fractal |
| Entry window | the 1m confirmation must be between 08:00 and 11:00 ET (fill before 11:00) | Model 2022 is usually executed "inside the London or New York morning killzones"; the exact hours are a project setting |
| Setup life | ends at the CME day boundary (18:00 ET), when it's used for an entry decision (even if blocked), or when price makes a new low after step 4 (the sweep low would be the stop) | **(choice)** |
| Positions | one at a time, no daily cap; closed at 16:00 ET if still open | no overnight holds is a **(choice)** for a day-trading test |
| Risk | 2% of equity per trade in the reported run, 1–100 MES contracts | project setting |
| Timeframes | 4h bias, 1h liquidity, 5m structure, 1m entry, all built from completed 1-minute bars | as above |

## Left out on purpose

- **SMT divergence.** An ICT confirmation tool, not a required step, and it
  needs a second market's data.
- **Order blocks, breakers, premium/discount and discretionary
  higher-timeframe levels** beyond the ones listed, and **scaling out** at
  several targets.
- **News filter.** Available with `--set smc.news_dates=2025-01-10,...`, off by default.

## What this is not

It's a mechanical version of a discretionary method. A result here means
"these rules did X on MES from START to END". It does not show whether ICT,
or anyone trading these concepts with judgment, makes money.
