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
    # A 50-day trend SMA and wider RSI levels give enough trades on two years of
    # random data to check every rule; the defaults are 200 / 10 / 90.
    cfg = SMARSIConfig(trend_sma=50, rsi_long=20, rsi_short=80)
    s = SMARSIStrategy(cfg, [r.time for r in rolls])
    res = run(bars, s, EngineConfig())
    return bars, s, res, cfg


def test_defaults_are_connors_rsi2():
    c = SMARSIConfig()
    assert (c.trend_sma, c.exit_sma, c.rsi_len, c.rsi_long, c.rsi_short) == (200, 5, 2, 10.0, 90.0)


def test_daily_indicators_match_groupby(setup):
    bars, s, _, cfg = setup
    daily = bars["close"].groupby(trading_day(bars.index)).last()
    ends = np.flatnonzero(s.day_end)
    if len(ends) < len(daily):          # the data stops mid-session on its last day
        daily = daily.iloc[:-1]
    assert len(ends) == len(daily)
    np.testing.assert_allclose(s.daily_close[ends], daily.to_numpy())
    np.testing.assert_allclose(s.sma_trend[ends], daily.rolling(cfg.trend_sma).mean().to_numpy(), equal_nan=True)
    np.testing.assert_allclose(s.sma_exit[ends], daily.rolling(cfg.exit_sma).mean().to_numpy(), equal_nan=True)
    np.testing.assert_allclose(s.rsi[ends], rsi(daily.to_numpy(), cfg.rsi_len), equal_nan=True)


def test_entries_follow_the_rules_and_fill_next_session_open(setup):
    bars, s, res, cfg = setup
    td = trading_day(bars.index)
    entries = [e for e in s.log if e[1] == "entry"]
    assert len(entries) >= 8
    times = {t.entry_time for t in res["trades"]}
    for i, _, d, qty in entries:
        assert s.day_end[i]
        if d == 1:
            assert s.daily_close[i] > s.sma_trend[i] and s.rsi[i] < cfg.rsi_long
        else:
            assert s.daily_close[i] < s.sma_trend[i] and s.rsi[i] > cfg.rsi_short
        if i + 1 >= len(bars):
            continue                   # signal on the data's last bar: nothing left to fill on
        assert td[i + 1] != td[i] and bars.index[i + 1] in times      # next session's first bar
        # 100% of equity in notional, sized on the real traded price
        assert qty == int(res["equity"].iloc[i] // (bars["raw_close"].iloc[i] * 5))


def test_exits_at_the_5_day_sma_and_flat_between_trades(setup):
    bars, s, res, _ = setup
    exits = {e[0]: e[2] for e in s.log if e[1] == "exit"}
    assert exits
    for i, d in exits.items():
        if d == 1:
            assert s.daily_close[i] > s.sma_exit[i]
        else:
            assert s.daily_close[i] < s.sma_exit[i]
    tr = res["trades"]
    for a, b in zip(tr, tr[1:]):
        assert b.entry_time > a.exit_time, "flat between trades: no reversal on the same open"
    assert all(t.exit_reason in ("close above 5-day SMA", "close below 5-day SMA", "end of data") for t in tr)


def test_long_only(setup):
    bars, _, _, cfg = setup
    import dataclasses
    s = SMARSIStrategy(dataclasses.replace(cfg, allow_shorts=False))
    res = run(bars, s, EngineConfig())
    assert res["trades"] and all(t.direction == 1 for t in res["trades"])


def test_sizing_uses_real_price_not_back_adjusted():
    """Back-adjusting shifts old prices by later roll gaps; contracts must be
    sized on what the contract actually traded at."""
    from backtest.strategies import BuyHoldStrategy
    mes, _ = make_pair(start="2023-01-08", end="2024-01-07", seed=11)    # 4 rolls, +12 pts each
    adj, rolls = back_adjust(mes)
    bars = resample(adj, "5min")
    assert (bars["close"].iloc[0] - bars["raw_close"].iloc[0]) == pytest.approx(sum(r.gap for r in rolls))
    s = BuyHoldStrategy([r.time for r in rolls])
    run(bars, s, EngineConfig())
    assert s.qty == int(100_000 // (bars["raw_close"].iloc[0] * 5))
