"""Exact checks for the daily SMA + RSI baseline against an independent daily resample."""
import numpy as np
import pandas as pd
import pytest

from backtest.data import back_adjust, resample, trading_day
from backtest.engine import EngineConfig, run
from backtest.indicators import rsi
from backtest.strategies import SMARSIConfig, SMARSIStrategy
from backtest.synthetic import make_pair


@pytest.fixture(scope="module")
def setup():
    mes, _ = make_pair(start="2023-01-08", end="2025-01-05", seed=11)
    adj, rolls = back_adjust(mes)
    bars = resample(adj, "5min")
    cfg = SMARSIConfig(rsi_low=45, rsi_high=55)
    s = SMARSIStrategy(cfg, [r.time for r in rolls])
    res = run(bars, s, EngineConfig())
    return bars, s, res, cfg


def test_daily_indicators_match_groupby(setup):
    bars, s, _, cfg = setup
    daily = bars["close"].groupby(trading_day(bars.index)).last()
    ends = np.flatnonzero(s.day_end)
    if len(ends) < len(daily):          # the data stops mid-session on its last day
        daily = daily.iloc[:-1]
    assert len(ends) == len(daily)
    np.testing.assert_allclose(bars["close"].to_numpy()[ends], daily.to_numpy())
    f = daily.rolling(cfg.sma_fast).mean().to_numpy()
    np.testing.assert_allclose(s.sma_fast[ends], f, equal_nan=True)
    np.testing.assert_allclose(s.rsi[ends], rsi(daily.to_numpy(), cfg.rsi_len), equal_nan=True)


def test_signals_follow_the_rules_and_fill_next_session_open(setup):
    bars, s, res, cfg = setup
    td = trading_day(bars.index)
    entries = [e for e in s.log if e[1] == "entry"]
    assert len(entries) >= 6
    times = {t.entry_time for t in res["trades"]}
    for i, _, d, qty in entries:
        assert s.day_end[i]
        if d == 1:
            assert s.sma_fast[i] > s.sma_slow[i] and s.rsi[i] < cfg.rsi_low
        else:
            assert s.sma_fast[i] < s.sma_slow[i] and s.rsi[i] > cfg.rsi_high
        # fills on the first bar of the next trading day
        assert td[i + 1] != td[i] and bars.index[i + 1] in times
        # 100% of equity (marked at this close) in notional, whole contracts
        assert qty == int(res["equity"].iloc[i] // (bars["close"].iloc[i] * 5))


def test_always_in_market_after_first_entry_and_reverses(setup):
    _, _, res, _ = setup
    tr = res["trades"]
    dirs = [t.direction for t in tr]
    assert all(a != b for a, b in zip(dirs, dirs[1:])), "each trade should reverse the previous one"
    for a, b in zip(tr, tr[1:]):
        assert a.exit_time == b.entry_time, "reversal closes and reopens at the same open"


def test_long_only_closes_instead_of_shorting(setup):
    bars, *_ = setup
    s = SMARSIStrategy(SMARSIConfig(rsi_low=45, rsi_high=55, allow_shorts=False))
    res = run(bars, s, EngineConfig())
    assert res["trades"] and all(t.direction == 1 for t in res["trades"])
    assert s.n_closes > 0
