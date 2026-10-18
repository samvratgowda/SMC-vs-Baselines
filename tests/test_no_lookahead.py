"""The most important test: no decision may depend on data from later bars.

For several cut-off points N we run each strategy on bars[:N] and on the full
data. Every order decision made on bars 0..N-1 must be identical in both runs.
If any code used future data (a resample that includes the current day, a
pivot read too early, a shift in the wrong direction), the decisions would
differ.
"""
import dataclasses

import numpy as np
import pandas as pd
import pytest

from backtest.data import back_adjust, resample
from backtest.engine import EngineConfig, run
from backtest.strategies import SMARSIConfig, SMARSIStrategy, TJRConfig, TJRStrategy
from backtest.synthetic import make_pair


@pytest.fixture(scope="module")
def data():
    mes, _ = make_pair(start="2024-01-07", end="2024-03-24", seed=3)
    return back_adjust(mes)[0]                            # 1-minute bars


def decisions(strategy, bars, cut):
    run(bars, strategy, EngineConfig())
    return [e for e in strategy.log if e[0] < cut]


def cuts_for(full, n_bars, max_cuts=12):
    """Cut right after decision bars: a leak only shows up if the strategy has
    to make a decision with the future removed. A cut at an arbitrary point
    only tests the last few bars before it, which is how a leak can hide."""
    entry_bars = sorted({e[0] for e in full if e[1] == "entry"})
    picks = entry_bars[:: max(1, len(entry_bars) // max_cuts)][:max_cuts]
    return sorted({b + 1 for b in picks} | {n_bars // 2})


def tjr_cuts(full, n_bars, per_kind=8):
    """For the TJR model, also cut right after each kind of state change (5m
    shift, pullback touch, invalidation). The stop and zone are set while the
    setup forms, long before the entry, so a leak there only shows up when the
    data ends during that stage."""
    cuts = set(cuts_for(full, n_bars))
    for kind in ("shift", "touch", "invalid"):
        bars = sorted({e[0] for e in full if e[1] == kind})
        cuts |= {b + 1 for b in bars[:: max(1, len(bars) // per_kind)][:per_kind]}
    return sorted(cuts)


# A wide window gives more decisions to compare on a few weeks of random data.
TJR = {
    "default": lambda: TJRStrategy(),
    "wide_window_no_bias": lambda: TJRStrategy(TJRConfig(start="03:00", end="15:00", use_bias=False)),
}


@pytest.mark.parametrize("name", list(TJR))
def test_tjr_decisions_never_use_later_bars(data, name):
    full = decisions(TJR[name](), data, len(data))
    assert len([e for e in full if e[1] == "entry"]) >= 8, "test needs some decisions to compare"
    for cut in tjr_cuts(full, len(data)):
        part = decisions(TJR[name](), data.iloc[:cut], cut)
        assert part == [e for e in full if e[0] < cut], f"decisions differ before bar {cut}"


def test_tjr_replay_after_every_state_change(data):
    """Cut after EVERY sweep, shift, touch, invalidation and entry on two weeks of data.
    Slower but exhaustive: a leak that only matters in a few setups still shows."""
    short = data.iloc[: 2 * 7 * 23 * 60]
    make = TJR["wide_window_no_bias"]
    full = decisions(make(), short, len(short))
    events = sorted({e[0] for e in full if e[1] in ("arm", "shift", "touch", "invalid", "entry")})
    assert len(events) >= 20
    for b in events:
        cut = b + 1
        assert decisions(make(), short.iloc[:cut], cut) == [e for e in full if e[0] < cut], f"differ before bar {cut}"


def _arrays(strategy, bars):
    from backtest.engine import Broker
    strategy.prepare(bars, Broker(bars, EngineConfig()))
    n = len(bars)
    return {k: v for k, v in vars(strategy).items()
            if isinstance(v, np.ndarray) and v.ndim == 1 and len(v) == n}


def test_tjr_precomputed_arrays_are_prefix_stable(data):
    """Every precomputed 1m series (previous-day levels, 4h bias, hourly swings,
    1m swings, session masks...) must have the same value at bar i whether or
    not later bars exist. This checks ALL bars up to each cut."""
    full = _arrays(TJRStrategy(), data)
    assert {"pdh", "pdl", "bias", "h1_sh", "h1_sl", "ph1", "flat_bar"} <= set(full)
    rng = np.random.default_rng(1)
    for cut in sorted(rng.integers(1000, len(data), 20)):
        part = _arrays(TJRStrategy(), data.iloc[:cut])
        for k, arr in part.items():
            np.testing.assert_array_equal(arr, full[k][:cut], err_msg=f"{k} changes when bars after {cut} exist")


def test_5m_bars_complete_only_after_their_last_minute(data):
    """A 5m bar must not be used before its last 1-minute bar has closed."""
    from backtest.strategies.smc import htf_groups
    g = htf_groups(data.index, *(data[k].to_numpy() for k in ("open", "high", "low", "close")), "5min")
    ok = g["avail"] >= 0
    end_of_bar = data.index[g["start"][ok]] + pd.Timedelta(minutes=5)
    known_at = data.index[g["avail"][ok]] + pd.Timedelta(minutes=1)     # close of the 1m bar
    assert (known_at >= end_of_bar).all()


# ---- daily SMA + RSI: needs 50+ trading days before its first signal ----

@pytest.fixture(scope="module")
def long_bars():
    mes, _ = make_pair(start="2023-01-08", end="2025-01-05", seed=11)
    return resample(back_adjust(mes)[0], "5min")


# Loose thresholds so a year of random data gives plenty of reversals to check.
LOOSE = SMARSIConfig(rsi_low=45, rsi_high=55)


def test_sma_rsi_no_lookahead(long_bars):
    bars = long_bars
    full = decisions(SMARSIStrategy(dataclasses.replace(LOOSE)), bars, len(bars))
    assert len([e for e in full if e[1] == "entry"]) >= 6
    for cut in cuts_for(full, len(bars), max_cuts=8):
        part = decisions(SMARSIStrategy(dataclasses.replace(LOOSE)), bars.iloc[:cut], cut)
        assert part == [e for e in full if e[0] < cut], f"decisions differ before bar {cut}"


def test_sma_rsi_arrays_are_prefix_stable(long_bars):
    bars = long_bars
    full = _arrays(SMARSIStrategy(), bars)
    assert {"day_end", "sma_fast", "sma_slow", "rsi"} <= set(full)
    rng = np.random.default_rng(2)
    for cut in sorted(rng.integers(300, len(bars), 15)):
        part = _arrays(SMARSIStrategy(), bars.iloc[:cut])
        for name, arr in part.items():
            # On early-close days the last bar of a truncated copy can't see that
            # the next bar is a new session. Every bar before it must match.
            n = cut - 1 if name == "day_end" else cut
            np.testing.assert_array_equal(arr[:n], full[name][:n], err_msg=f"{name} changes when bars after {cut} exist")
