"""Exact checks for turning trades into order-flow bars and volume profiles."""
import numpy as np
import pandas as pd

from backtest.orderflow import aggregate_trades, low_volume_nodes, value_area


def T(ts, price, size, side, iid=1):
    return {"ts_event": pd.Timestamp(ts, tz="UTC"), "price": price, "size": size, "side": side, "instrument_id": iid}


def test_bars_delta_and_aggressive_orders():
    trades = pd.DataFrame([
        T("2024-03-05 14:30:01.000", 100.00, 2, "B"),
        T("2024-03-05 14:30:01.000", 100.25, 3, "B"),   # same timestamp + side: one order of 5
        T("2024-03-05 14:30:20.000", 99.75, 1, "A"),
        T("2024-03-05 14:30:40.000", 100.00, 4, "N"),   # unknown side: volume only
        T("2024-03-05 14:31:05.000", 100.50, 7, "A"),
    ])
    bars, vap = aggregate_trades(trades)
    b0 = bars.iloc[0]
    assert (b0.open, b0.high, b0.low, b0.close, b0.volume) == (100.0, 100.25, 99.75, 100.0, 10)
    assert (b0.buy_vol, b0.sell_vol) == (5, 1)
    assert (b0.max_buy_order, b0.max_sell_order) == (5, 1)
    b1 = bars.iloc[1]
    assert (b1.buy_vol, b1.sell_vol, b1.max_sell_order) == (0, 7, 7)
    v0 = vap[vap.ts_event == bars.index[0]].set_index("tick")["volume"]
    assert v0.to_dict() == {399: 1, 400: 6, 401: 3}           # ticks of 0.25


def test_stacked_footprint_imbalances():
    rows = []
    ts = "2024-03-05 14:30:10"
    # buyers 10 at 100.00, 100.25, 100.50; sellers 2 at 99.75, 100.00, 100.25
    for p in (100.00, 100.25, 100.50):
        rows.append(T(ts, p, 10, "B"))
    for p in (99.75, 100.00, 100.25):
        rows.append(T(ts, p, 2, "A"))
    bars, _ = aggregate_trades(pd.DataFrame(rows))
    # buy imbalance at p needs buy[p] >= 3 * sell[p - 1 tick]: true at all three -> stack of 3
    assert bars.iloc[0].buy_stack == 3
    assert bars.iloc[0].sell_stack == 0


def test_value_area():
    ticks = np.arange(10)
    vols = np.array([1, 1, 2, 5, 9, 6, 2, 1, 1, 1], float)    # total 29, 70% = 20.3
    poc, val, vah = value_area(ticks, vols)
    # POC 4 (9); add 5 (6) -> 15; add 3 (5) -> 20; then 6 (2) vs 2 (2): tie goes up -> 22
    assert (poc, val, vah) == (4, 3, 6)


def test_low_volume_node_between_two_high_volume_areas():
    prof = {k: 20 for k in range(0, 8)} | {k: 1 for k in range(8, 12)} | {k: 20 for k in range(12, 20)}
    lv = low_volume_nodes(prof, 0, 19)
    assert lv and all(8 <= k <= 11 for k in lv)
    assert low_volume_nodes({k: 10 for k in range(20)}, 0, 19) == []   # flat profile: no nodes
