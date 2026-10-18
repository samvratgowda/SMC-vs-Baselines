import numpy as np
import pandas as pd
import pytest

from backtest.data import back_adjust, resample, trading_day
from backtest.indicators import pivots, rsi


def test_back_adjust_removes_roll_gap_and_keeps_latest_prices():
    idx = pd.date_range("2024-03-14 14:00", periods=4, freq="1min", tz="UTC")
    df = pd.DataFrame({"open": [100, 101, 151, 152], "high": [101, 102, 152, 153],
                       "low": [99, 100, 150, 151], "close": [101, 101, 152, 152],
                       "instrument_id": [1, 1, 2, 2]}, index=idx)
    adj, rolls = back_adjust(df)
    assert len(rolls) == 1 and rolls[0].gap == 50
    assert adj["close"].iloc[1] == 151                       # old contract shifted up by the gap
    assert adj["open"].iloc[2] == 151                        # no jump across the roll
    assert (adj.iloc[2:][["open", "close"]].to_numpy() == df.iloc[2:][["open", "close"]].to_numpy()).all()


def test_resample_5m_ohlc():
    idx = pd.date_range("2024-01-02 14:00", periods=10, freq="1min", tz="UTC")
    df = pd.DataFrame({"open": np.arange(10.), "high": np.arange(10.) + 1,
                       "low": np.arange(10.) - 1, "close": np.arange(10.) + .5}, index=idx)
    r = resample(df, "5min")
    assert list(r["open"]) == [0, 5] and list(r["high"]) == [5, 10]
    assert list(r["low"]) == [-1, 4] and list(r["close"]) == [4.5, 9.5]


def test_trading_day_rolls_at_6pm_eastern():
    t = pd.DatetimeIndex(["2024-01-02 17:55", "2024-01-02 18:00"]).tz_localize("America/New_York").tz_convert("UTC")
    d = trading_day(t)
    assert str(d[0])[:10] == "2024-01-02" and str(d[1])[:10] == "2024-01-03"


def test_pivots_confirm_late_and_never_look_ahead():
    h = np.array([1, 2, 5, 3, 2, 1, 1, 9.])
    l = h - 1
    ph, _ = pivots(h, l, 2, 2)
    assert np.isnan(ph[:4]).all()            # the peak at bar 2 is unknown until bar 4
    assert ph[4] == 5
    # Changing a future bar never changes an already-confirmed value.
    h2 = h.copy(); h2[-1] = -1
    ph2, _ = pivots(h2, h2 - 1, 2, 2)
    assert np.array_equal(np.nan_to_num(ph[:6]), np.nan_to_num(ph2[:6]))


def test_rsi_bounds():
    c = np.cumsum(np.random.default_rng(0).standard_normal(300)) + 100
    r = rsi(c, 14)
    assert np.nanmin(r) >= 0 and np.nanmax(r) <= 100
