"""Engine fills on hand-made bars where the right answer is known exactly."""
import pandas as pd
import pytest

from backtest.engine import EngineConfig, run


def bars(rows):
    idx = pd.date_range("2024-01-02 14:00", periods=len(rows), freq="5min", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


class Script:
    """Strategy that replays a list of (bar, callable) actions."""
    name = "script"

    def __init__(self, actions):
        self.actions = dict(actions)

    def prepare(self, b, broker):
        pass

    def on_bar(self, i, broker):
        if i in self.actions:
            self.actions[i](broker)


CFG = EngineConfig(point_value=5, tick=0.25, commission=1.0, slippage_ticks=1, initial_capital=10_000)


def test_market_order_fills_next_open_with_slippage_and_hits_target():
    b = bars([[100, 101, 99, 100], [100, 100.5, 99.5, 100], [100, 106, 99.75, 105]])
    res = run(b, Script([(0, lambda br: br.market_entry(1, 2, stop=98, target=105))]), CFG)
    t = res["trades"][0]
    assert t.entry_time == b.index[1] and t.entry_price == 100.25          # open + 1 tick
    assert t.exit_price == 105 and t.exit_reason == "target"                # no slippage on target
    assert t.pnl == pytest.approx((105 - 100.25) * 2 * 5 - 4 * 1.0)         # 4 contract-sides of commission
    assert res["equity"].iloc[-1] == pytest.approx(10_000 + t.pnl)


def test_conservative_mode_takes_stop_when_bar_touches_both():
    b = bars([[100, 100, 100, 100], [100, 100, 100, 100], [100, 106, 97, 101]])
    res = run(b, Script([(0, lambda br: br.market_entry(1, 1, stop=98, target=105))]), CFG)
    assert res["trades"][0].exit_reason == "stop"
    assert res["trades"][0].exit_price == 97.75                             # stop - 1 tick slippage


def test_tv_mode_uses_open_proximity():
    # open 100 is closer to the high (101) than the low (95): path O-H-L-C, target 101 first.
    b = bars([[100, 100, 100, 100], [100, 100, 100, 100], [100, 101, 95, 96]])
    cfg = EngineConfig(**{**CFG.__dict__, "intrabar": "tv"})
    res = run(b, Script([(0, lambda br: br.market_entry(1, 1, stop=98, target=101))]), cfg)
    assert res["trades"][0].exit_reason == "target"


def test_gap_through_stop_fills_at_open():
    b = bars([[100, 100, 100, 100], [100, 100, 100, 100], [95, 96, 94, 95]])
    res = run(b, Script([(0, lambda br: br.market_entry(1, 1, stop=98, target=110))]), CFG)
    t = res["trades"][0]
    assert t.exit_reason == "stop (gap)" and t.exit_price == 94.75


def test_limit_entry_fills_at_limit_then_can_stop_same_bar():
    b = bars([[100, 100, 100, 100], [100, 100.5, 96, 97]])
    res = run(b, Script([(0, lambda br: br.limit_entry(1, 99, 1, stop=97, target=103))]), CFG)
    t = res["trades"][0]
    assert t.entry_price == 99 and t.exit_reason == "stop" and t.exit_price == 96.75


def test_limit_entry_fills_at_better_open():
    b = bars([[100, 100, 100, 100], [98, 99, 97.5, 98.5]])
    res = run(b, Script([(0, lambda br: br.limit_entry(1, 99, 1, stop=96, target=110))]), CFG)
    assert res["trades"][0].entry_price == 98


def test_short_side_and_flatten():
    b = bars([[100, 100, 100, 100], [100, 100, 99, 99.5], [99, 99.5, 98.5, 99], [98, 98, 98, 98]])
    acts = [(0, lambda br: br.market_entry(-1, 1, stop=102, target=90)), (2, lambda br: br.flatten())]
    res = run(b, Script(acts), CFG)
    t = res["trades"][0]
    assert t.entry_price == 99.75 and t.exit_reason == "flatten" and t.exit_price == 98.25
    assert t.pnl == pytest.approx((99.75 - 98.25) * 5 - 2)
