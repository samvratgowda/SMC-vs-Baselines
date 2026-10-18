"""A hand-built 1-minute price path with known answers for the TJR model.

Two CME trading days, flat at 100 except where a scenario sets a bar.
Day 1 (Mon) makes the 4h bias bullish (a 4h close at 104 above the 4h swing
high at 103) and sets the previous-day high/low for day 2 (104.25 / 95).
Day 2 (Tue) plays out every step of the model, worked out by hand below.
"""
import pandas as pd

from backtest.engine import EngineConfig, run, trades_frame
from backtest.strategies import TJRConfig, TJRStrategy

ET = "America/New_York"
DAY1 = {
    "2024-03-04 03:00": (100, 103, 99.75, 100),          # 4h swing high 103 (02:00-06:00 bucket)
    "2024-03-04 11:00": (100, 100.25, 95, 100),          # previous-day low 95
    "2024-03-04 16:59": (104, 104.25, 103.75, 104),      # 4h close 104 > 103: bias bullish
}
DAY2 = {
    "2024-03-05 08:42": (100, 100.75, 99.9, 100),        # 5m swing high 100.75 (also takes Asia/London H)
    # Down-leg: sweeps Asia L / London L (99.75) at 09:00, low 98.0
    "2024-03-05 09:00": (100, 100.1, 99.5, 99.6),
    "2024-03-05 09:01": (99.6, 99.6, 99.0, 99.1),
    "2024-03-05 09:02": (99.1, 99.2, 98.6, 98.7),
    "2024-03-05 09:03": (98.7, 98.8, 98.2, 98.3),
    "2024-03-05 09:04": (98.3, 98.4, 98.0, 98.1),
    "2024-03-05 09:05": (98.1, 98.5, 98.05, 98.4),
    "2024-03-05 09:06": (98.4, 98.9, 98.3, 98.8),
    "2024-03-05 09:07": (98.8, 99.3, 98.7, 99.2),
    "2024-03-05 09:08": (99.2, 99.7, 99.1, 99.6),
    "2024-03-05 09:09": (99.6, 100.1, 99.5, 100.0),
    # 09:10-09:14 5m bar closes 101.1 above 100.75: 5m BOS, known at 09:14
    "2024-03-05 09:10": (100.0, 100.6, 99.9, 100.5),
    "2024-03-05 09:11": (100.5, 100.9, 100.4, 100.8),
    "2024-03-05 09:12": (100.8, 101.0, 100.7, 100.9),
    "2024-03-05 09:13": (100.9, 101.1, 100.8, 101.0),
    "2024-03-05 09:14": (101.0, 101.2, 100.9, 101.1),
    # Leg high 101.5 -> equilibrium (98.0 + 101.5) / 2 = 99.75; no 5m FVG in the leg
    "2024-03-05 09:15": (101.1, 101.5, 101.0, 101.4),
    "2024-03-05 09:16": (101.4, 101.45, 100.8, 100.9),
    "2024-03-05 09:17": (100.9, 101.0, 100.3, 100.4),
    "2024-03-05 09:18": (100.4, 100.5, 99.9, 100.0),     # 1m bearish FVG 100.8 / 100.5
    "2024-03-05 09:19": (100.0, 100.1, 99.7, 99.8),      # touches EQ 99.75; newer 1m bearish FVG 100.3 / 100.1
    "2024-03-05 09:20": (99.8, 100.0, 99.75, 99.95),
    "2024-03-05 09:21": (99.95, 100.45, 99.9, 100.4),    # closes above 100.3: 1m inverse FVG -> buy
    "2024-03-05 09:22": (100.4, 100.8, 100.3, 100.7),    # filled at the open + 1 tick = 100.65
    "2024-03-05 09:23": (100.7, 104.5, 100.6, 104.4),    # reaches the target 104.25 (previous-day high)
}


def build(day1=DAY1, day2=DAY2) -> pd.DataFrame:
    idx = pd.date_range(pd.Timestamp("2024-03-03 18:00", tz=ET), pd.Timestamp("2024-03-05 17:00", tz=ET),
                        freq="1min", inclusive="left")
    idx = idx[idx.hour != 17]                            # daily 17:00-18:00 break
    sets = {pd.Timestamp(k, tz=ET): v for k, v in {**day1, **day2}.items()}
    last2 = pd.Timestamp(max(day2), tz=ET)
    tail = day2[max(day2)][3]
    rows = []
    for t in idx:
        if t in sets:
            rows.append(sets[t])
        elif t > last2:
            rows.append((tail, tail + 0.25, tail - 0.25, tail))
        else:
            rows.append((100.0, 100.25, 99.75, 100.0))
    df = pd.DataFrame(rows, index=idx.tz_convert("UTC"), columns=["open", "high", "low", "close"], dtype=float)
    df["volume"] = 1
    return df


def ts(s):
    return pd.Timestamp(s, tz=ET).tz_convert("UTC")


def test_every_step_of_the_model_on_a_known_path():
    s = TJRStrategy()
    tf = trades_frame(run(build(), s, EngineConfig())["trades"])
    assert len(tf) == 1
    t = tf.iloc[0]
    assert t.direction == "long" and t.bias_4h == 1
    assert t.levels_swept == "Asia L+London L" and t.sweep_time == ts("2024-03-05 09:00")
    assert t.sweep_extreme == 98.0 and t.stop == 98.0
    assert t.order_flow == "BOS" and t.order_flow_time == ts("2024-03-05 09:14")
    assert t.zone == "EQ" and t.zone_price == 99.75
    assert t.entry_confirmation == "IFVG" and t.signal_time == ts("2024-03-05 09:21")
    assert t.entry_time == ts("2024-03-05 09:22") and t.entry_price == 100.65
    assert t.target_level in ("PDH", "1h swing H") and t.target == 104.25
    assert t.exit_reason == "target" and t.exit_time == ts("2024-03-05 09:23")
    f = s.funnel()
    # Day 1 also has a long shift (its 11:00 sweep, broken by the 16:59 close); that
    # setup expires at the 18:00 day boundary, so only day 2's becomes a trade.
    assert f["5m order-flow shift (BOS/IFVG)"]["long"] == 2
    assert f["Pulled back into FVG/EQ"]["long"] == 1 and f["Orders placed"]["long"] == 1


def test_no_long_without_bullish_4h_bias():
    day1 = dict(DAY1)
    day1["2024-03-04 16:59"] = (100, 100.25, 99.75, 100)   # no 4h close above the swing high
    s = TJRStrategy()
    assert run(build(day1=day1), s, EngineConfig())["trades"] == []
    assert s.funnel()["Against 4h bias"]["long"] == 1
    s2 = TJRStrategy(TJRConfig(use_bias=False))
    assert len(run(build(day1=day1), s2, EngineConfig())["trades"]) == 1


def test_new_low_after_the_shift_invalidates_the_setup():
    day2 = dict(DAY2)
    day2["2024-03-05 09:20"] = (99.8, 100.0, 97.9, 99.95)  # trades below the 98.0 sweep low
    s = TJRStrategy()
    assert run(build(day2=day2), s, EngineConfig())["trades"] == []
    assert s.funnel()["Invalidated: new extreme before entry"]["long"] == 1


def test_confirmation_outside_entry_window_is_not_traded():
    s = TJRStrategy(TJRConfig(start="09:30", end="11:00"))
    assert run(build(), s, EngineConfig())["trades"] == []
    assert s.funnel()["Outside entry window"]["long"] == 1


def test_target_must_be_at_least_min_r_away():
    # Risk is 2.4 points (100.4 - 98.0); the nearest untaken high is 4.1 points away.
    s = TJRStrategy(TJRConfig(min_target_rr=2.0))
    assert run(build(), s, EngineConfig())["trades"] == []
    assert s.funnel()["No liquidity target >= min R"]["long"] == 1
