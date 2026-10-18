"""The random-entry control must fill trades exactly like the engine does."""
import numpy as np
import pandas as pd
import pytest

from backtest.data import back_adjust
from backtest.engine import EngineConfig, run
from backtest.random_control import RandomTrades
from backtest.strategies.base import Strategy
from backtest.synthetic import make_pair


@pytest.fixture(scope="module")
def bars():
    mes, _ = make_pair(start="2024-01-07", end="2024-03-03", seed=9)
    return back_adjust(mes)[0]


class OneTrade(Strategy):
    """Places one market order on bar t-1 (fills at t's open) with given stop/target,
    and flattens so the exit happens at the open of bar x, like the control."""
    def __init__(self, t, x, d, stop, target):
        self.t, self.x, self.d, self.stop, self.target = t, x, d, stop, target

    def prepare(self, bars, broker):
        pass

    def on_bar(self, i, b):
        if i == self.t - 1:
            b.market_entry(self.d, 1, self.stop, self.target)
        if i == self.x - 1 and b.position != 0:
            b.flatten()


def test_matches_the_engine_trade_for_trade(bars):
    rt_ = RandomTrades(bars)
    cfg = EngineConfig()
    rng = np.random.default_rng(0)
    tick = cfg.tick
    checked = 0
    for _ in range(60):
        c = rt_.days[rng.integers(0, len(rt_.days))]
        t = int(c[rng.integers(0, len(c))])
        d = int(rng.choice([-1, 1]))
        sp, tr = float(rng.choice([1.0, 2.5, 4.0, 8.0])), float(rng.choice([1.0, 1.5, 3.0]))
        x = rt_.exit_idx[t]
        fill = rt_.o[t] + d * tick
        stop = round((fill - d * sp) / tick) * tick
        target = round((fill + d * tr * sp) / tick) * tick
        res = run(bars.iloc[: x + 2], OneTrade(t, x, d, stop, target), cfg)
        assert len(res["trades"]) == 1
        assert rt_.trade_r(t, d, sp, tr) == pytest.approx(res["trades"][0].r_multiple, abs=1e-9)
        checked += 1
    assert checked == 60


def test_stop_wins_when_one_bar_touches_both():
    idx = pd.date_range(pd.Timestamp("2024-03-05 08:00", tz="America/New_York"), periods=500, freq="1min").tz_convert("UTC")
    df = pd.DataFrame({"open": 100.0, "high": 100.25, "low": 99.75, "close": 100.0}, index=idx)
    df.iloc[1, df.columns.get_loc("high")] = 110.0     # entry bar reaches both stop and target
    df.iloc[1, df.columns.get_loc("low")] = 90.0
    r = RandomTrades(df).trade_r(1, 1, 2.0, 2.0)
    fill, stop = 100.25, 98.25
    assert r == pytest.approx(((stop - 0.25 - fill) * 5 - 2) / ((fill - stop) * 5))


def test_runs_are_reproducible_and_matched_to_the_trades(bars):
    rt_ = RandomTrades(bars)
    fake = pd.DataFrame({"entry_price": [100.0, 100.0, 100.0], "stop": [98.0, 97.0, 99.0],
                         "target": [103.0, 106.0, 101.0], "r": [-1.0, 0.5, 1.2]})
    a, b = rt_.run(fake, n_runs=20, seed=4), rt_.run(fake, n_runs=20, seed=4)
    assert np.array_equal(a.run_avg_r, b.run_avg_r) and a.n_trades == 3
    assert a.strategy_avg_r == pytest.approx(np.mean([-1.0, 0.5, 1.2]))
    assert 0.0 <= a.pct_below <= 1.0
