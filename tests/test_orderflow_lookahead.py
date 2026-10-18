"""No decision of the order-flow strategy may depend on later bars.

Same two checks as for the SMC model: replay with the data cut right after
every state change and entry, and prefix-stability of every precomputed
series (previous-day value area, big-order threshold, aggression flags...).
"""
import numpy as np
import pytest

from backtest.engine import Broker, EngineConfig, run
from backtest.orderflow import aggregate_trades, prepare_orderflow
from backtest.strategies.orderflow import FabioConfig, FabioStrategy
from backtest.synthetic import make_pair, make_trades


@pytest.fixture(scope="module")
def of_data():
    mes, _ = make_pair(start="2024-01-07", end="2024-03-03", seed=4)
    bars, vap, _ = prepare_orderflow(*aggregate_trades(make_trades(mes, seed=4)))
    return bars, vap


WIDE = FabioConfig(start="03:00", end="15:00")


def decisions(bars, vap, cut, cfg=WIDE):
    s = FabioStrategy(vap, cfg)
    run(bars.iloc[:cut], s, EngineConfig())
    return [e for e in s.log if e[0] < cut]


def test_orderflow_replay_after_every_state_change(of_data):
    bars, vap = of_data
    n = 3 * 7 * 23 * 60                                   # three weeks, every event
    full = decisions(bars, vap, n)
    entries = [e for e in full if e[1] == "entry"]
    assert len(entries) >= 5
    events = {e[0] for e in full if e[1] in ("arm", "invalid", "entry")}
    # Leg extensions are frequent; a sample of them still catches a profile that
    # peeks ahead, because the logged profile total would differ.
    ext = sorted({e[0] for e in full if e[1] == "extend"})
    events |= set(ext[:: max(1, len(ext) // 40)])
    for b in sorted(events):
        cut = b + 1
        assert decisions(bars, vap, cut) == [e for e in full if e[0] < cut], f"decisions differ before bar {cut}"


def test_orderflow_arrays_are_prefix_stable(of_data):
    bars, vap = of_data

    def arrays(cut):
        s = FabioStrategy(vap, FabioConfig())
        sub = bars.iloc[:cut]
        s.prepare(sub, Broker(sub, EngineConfig()))
        return {k: v for k, v in vars(s).items() if isinstance(v, np.ndarray) and v.ndim == 1 and len(v) == cut}

    full = arrays(len(bars))
    assert {"poc_prev", "val_prev", "vah_prev", "big_thr", "agg_long", "agg_short", "vstart", "vend"} <= set(full)
    rng = np.random.default_rng(3)
    for cut in sorted(rng.integers(5000, len(bars), 12)):
        part = arrays(cut)
        for k, arr in part.items():
            np.testing.assert_array_equal(arr, full[k][:cut], err_msg=f"{k} changes when bars after {cut} exist")
