"""A hand-built trade tape with a known answer for the Fabio mean-reversion model.

Day 1 (Mon) trades the same profile every minute, so its value area is known:
POC 101.00, VAL 100.75, VAH 101.50 (worked out in test_value_area's method).
Day 2 (Tue) pushes below VAL, fails, reclaims value, pulls back into a
low-volume node of the reclaim leg and shows stacked buy imbalances there.
Every step is worked out by hand in the comments.
"""
import pandas as pd

from backtest.engine import EngineConfig, run, trades_frame
from backtest.orderflow import aggregate_trades, prepare_orderflow
from backtest.strategies.orderflow import FabioConfig, FabioStrategy

ET = "America/New_York"
DAY1 = [(101.00, 3, "B"), (100.25, 1, "A"), (100.50, 2, "B"), (100.75, 3, "A"), (101.25, 3, "B"),
        (101.50, 2, "A"), (101.75, 1, "B"), (101.00, 3, "A")]          # POC 101.00, VA 100.75-101.50
DAY2_FLAT = [(101.00, 3, "B"), (101.00, 3, "A")]
SPLIT = lambda p, n: [(p, n, "B"), (p, n, "A")]
SCENARIO = {
    "09:00": [(101.00, 1, "B"), (100.75, 1, "A"), (100.50, 1, "A")],   # trades below VAL 100.75
    "09:01": [(100.50, 1, "A"), (100.00, 1, "A"), (99.50, 1, "A")],
    "09:02": [(99.50, 1, "A"), (99.00, 1, "A"), (98.75, 1, "A")],
    "09:03": [(98.75, 1, "A"), (98.50, 1, "A"), (98.75, 1, "B")],      # low outside value: 98.50
    **{f"09:0{m}": SPLIT(98.75, 8) + SPLIT(99.00, 8) for m in (4, 5, 6, 7)},   # heavy volume 98.75-99.00
    "09:08": [(99.25, 1, "B"), (99.50, 1, "B"), (99.75, 1, "B"), (100.00, 1, "B")],   # thin: the LVN
    # heavy 100.25-101.00; the 09:05-09:09 5m candle closes 101.00 > VAL: reclaimed
    **{f"09:{m:02d}": SPLIT(100.25, 8) + SPLIT(100.50, 8) + SPLIT(100.75, 8) + SPLIT(101.00, 8)
       for m in (9, 10, 11, 12, 13)},
    "09:14": [(100.75, 1, "A"), (100.25, 1, "A"), (100.00, 1, "A")],   # pullback, no aggression
    # At the LVN: buyers 10 at 99.50/99.75/100.00 vs sellers 2 one tick below -> 3 stacked imbalances.
    "09:15": [(100.00, 1, "A"), (99.25, 2, "A"), (99.50, 2, "A"), (99.75, 2, "A"),
              (99.50, 10, "B"), (99.75, 10, "B"), (100.00, 10, "B"), (99.75, 1, "B")],
    "09:16": [(99.75, 1, "B"), (100.00, 1, "B")],                      # filled at the open 99.75 + 1 tick
    "09:17": [(99.75, 1, "B"), (101.25, 1, "B"), (101.00, 1, "B")],    # reaches the POC target 101.00
}


def tape(scenario=SCENARIO) -> pd.DataFrame:
    idx = pd.date_range(pd.Timestamp("2024-03-03 18:00", tz=ET), pd.Timestamp("2024-03-05 17:00", tz=ET),
                        freq="1min", inclusive="left")
    idx = idx[idx.hour != 17]
    day2 = pd.Timestamp("2024-03-04 18:00", tz=ET)
    rows = []
    for t in idx:
        key = t.strftime("%H:%M") if t.date() == pd.Timestamp("2024-03-05").date() else None
        trades = scenario.get(key) if key else None
        if trades is None:
            trades = DAY1 if t < day2 else DAY2_FLAT
        for k, (p, sz, side) in enumerate(trades):
            rows.append({"ts_event": t.tz_convert("UTC") + pd.Timedelta(milliseconds=100 * k + 1),
                         "price": p, "size": sz, "side": side, "instrument_id": 1})
    return pd.DataFrame(rows)


def ts(s):
    return pd.Timestamp(f"2024-03-05 {s}", tz=ET).tz_convert("UTC")


def run_tape(scenario=SCENARIO, cfg=None):
    bars, vap, _ = prepare_orderflow(*aggregate_trades(tape(scenario)))
    s = FabioStrategy(vap, cfg or FabioConfig())
    return trades_frame(run(bars, s, EngineConfig())["trades"]), s


def test_mean_reversion_trade_on_a_known_tape():
    tf, s = run_tape()
    assert len(tf) == 1
    t = tf.iloc[0]
    assert t.direction == "long" and t.model == "mean reversion"
    assert (t.balance_val, t.balance_vah, t.balance_poc) == (100.75, 101.50, 101.00)
    assert t.leg_start_time == ts("09:03") and t.leg_origin == 98.50 and t.leg_extreme == 101.00
    # Leg profile (98.50..101.00): smoothed minima at 99.50 and 99.75; the bar reaches 99.75 first
    assert t.lvn_price == 99.75 and t.aggression == "stacked imbalance"
    assert t.signal_time == ts("09:15")
    assert t.entry_time == ts("09:16") and t.entry_price == 100.00
    assert t.stop == 98.75                      # min(low 99.25, LVN 99.75) - 2 ticks
    assert t.target == 101.00 and t.exit_reason == "target" and t.exit_time == ts("09:17")


def test_no_trade_without_aggression():
    sc = dict(SCENARIO)
    sc["09:15"] = [(100.00, 1, "A"), (99.25, 2, "A"), (99.50, 2, "B"), (99.75, 2, "B"), (100.00, 2, "B"), (99.75, 1, "B")]
    tf, s = run_tape(sc)
    assert tf.empty
    assert s.funnel()["Aggression at an LVN"]["long"] == 0


def test_trigger_bar_closing_through_the_lvn_is_ignored():
    sc = dict(SCENARIO)
    sc["09:15"] = SCENARIO["09:15"][:-1] + [(99.00, 1, "A")]           # closes 99.00, through the node
    tf, _ = run_tape(sc)
    assert tf.empty


def test_new_low_invalidates_the_reclaim_leg():
    sc = dict(SCENARIO)
    sc["09:14"] = [(100.75, 1, "A"), (99.00, 1, "A"), (98.25, 1, "A"), (99.75, 1, "B")]   # below 98.50
    tf, s = run_tape(sc)
    assert tf.empty
    assert s.funnel()["Invalidated: leg extreme broken"]["long"] >= 1


def test_outside_entry_window_is_not_traded():
    tf, s = run_tape(cfg=FabioConfig(start="09:30", end="11:00"))
    assert tf.empty and s.funnel()["Outside entry window"]["long"] == 1
