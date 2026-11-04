"""交易規則、當沖、波段、模擬下單、觀察名單的測試（模擬資料）。"""
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trader import daytrade, settings, swing, twse  # noqa: E402
from trader.paper_broker import PaperBroker  # noqa: E402
from trader.universe import watchlist  # noqa: E402


def cfg(**over):
    c = settings.load(Path(tempfile.mkdtemp()) / "none.yaml")
    for k, v in over.items():
        c = settings.set_value(c, k.replace("__", "."), v)
    return c


# ── 台股規則 ──
def test_ticks_and_limits():
    assert [twse.tick_size(p) for p in (9.99, 10, 49.95, 50, 99.9, 100, 499.5, 500, 999, 1000)] == \
        [0.01, 0.05, 0.05, 0.1, 0.1, 0.5, 0.5, 1, 1, 5]
    assert twse.round_to_tick(101.3, "up") == 101.5 and twse.round_to_tick(101.3, "down") == 101.0
    assert twse.round_to_tick(9.995, "up") == 10.0
    assert twse.ticks_away(10.0, -1) == 9.99 and twse.ticks_away(9.99, 1) == 10.0
    assert twse.ticks_away(100.0, 1) == 100.5 and twse.ticks_away(100.0, -1) == 99.9
    assert twse.limit_prices(100) == (110.0, 90.0)
    assert twse.limit_prices(47.3) == (52.0, 42.6)


def test_costs():
    c = cfg()["costs"]
    assert twse.trade_cost(100_000, "buy", c, True) == 142          # 0.1425%
    assert twse.trade_cost(100_000, "sell", c, True) == 142 + 150   # 加當沖稅 0.15%
    assert twse.trade_cost(100_000, "sell", c, False) == 142 + 300
    assert twse.trade_cost(5_000, "buy", c, False) == 20            # 最低手續費


# ── 當沖 ──
def day(rows, date="2026-11-02"):
    """rows：[(HH:MM, open, high, low, close, volume_lots)]，5 分 K。"""
    idx = pd.to_datetime([f"{date} {r[0]}" for r in rows])
    return pd.DataFrame([r[1:] for r in rows], index=idx,
                        columns=["open", "high", "low", "close", "volume"]).assign(volume=lambda d: d.volume * 1000)


OR = [("09:00", 100, 101, 99.5, 100.5, 500), ("09:05", 100.5, 101, 99.5, 100, 300), ("09:10", 100, 100.5, 99.5, 100, 200)]
INFO = {"A": {"prev_close": 100.0, "avg_vol50": 5_000_000}}


def test_long_take_profit():
    bars = day(OR + [("09:15", 100, 101.5, 100, 101.5, 400), ("09:20", 101.5, 104, 101, 103.5, 300),
                     ("09:25", 103.5, 109, 103, 108.5, 300), ("09:30", 108.5, 109, 108, 108.5, 100)])
    (t,), st = daytrade.run_day({"A": bars}, INFO, cfg())
    assert t.entry == 102.0                # 區間高 101 + 1 檔 = 101.5，再加 1 檔滑價
    assert t.stop == 100.0                 # 2% 停損 99.96 向上取檔 100.0，比區間低點 99.5 近
    assert t.target == 108.5               # 6% 停利 108.12 向上取檔
    assert t.reason == "停利" and t.exit == 108.5
    assert t.lots == 2                     # min(30 萬 ÷ 10.2 萬, 1 萬 ÷ 每張風險 2000) → 2 張
    assert t.pnl == (108.5 - 102) * 2000 - t.cost


def test_stop_first_when_both_hit_same_bar():
    bars = day(OR + [("09:15", 100, 101.5, 100, 101.5, 400), ("09:20", 101.5, 109, 98, 100, 300)])
    (t,), _ = daytrade.run_day({"A": bars}, INFO, cfg())
    assert t.reason == "停損" and t.exit == 99.9  # 停損 100.0 再少 1 檔


def test_force_close_and_entry_window():
    bars = day(OR + [("10:05", 100, 101.5, 100, 101.5, 400)])          # 10:00 後才突破 → 不進場
    assert daytrade.run_day({"A": bars}, INFO, cfg())[0] == []
    bars = day(OR + [("09:15", 100, 101.5, 100, 101.5, 400), ("13:05", 102, 102.5, 101.5, 102, 50),
                     ("13:10", 102, 102.5, 101.5, 102, 50)])
    (t,), _ = daytrade.run_day({"A": bars}, INFO, cfg())
    assert t.reason == "強制平倉" and t.exit_time == "13:10" and t.exit == 101.5


def test_short_and_filters():
    bars = day(OR + [("09:15", 100, 100, 99, 99, 400), ("09:20", 99, 99.5, 93, 93.2, 300)])
    (t,), _ = daytrade.run_day({"A": bars}, INFO, cfg(daytrade__direction="short"))
    assert t.direction == "short" and t.entry == 99.3 and t.stop == 101.0
    assert t.reason == "停利" and t.exit == 93.3
    # 區間太窄（< 0.8%）→ 不交易
    narrow = day([("09:00", 100, 100.2, 99.9, 100, 500), ("09:15", 100, 101, 100, 101, 400)])
    tr, st = daytrade.run_day({"A": narrow}, INFO, cfg())
    assert tr == [] and st["skipped"] == {"開盤區間寬度不符": 1}
    # 接近漲停 → 不進場
    tr, st = daytrade.run_day({"A": day(OR + [("09:15", 100, 101.5, 100, 101.5, 400)])},
                              {"A": {"prev_close": 92.5, "avg_vol50": 5_000_000}}, cfg())
    assert tr == [] and "接近漲跌停" in st["skipped"]


# ── 模擬下單 ──
def test_paper_broker():
    ev = []
    b = PaperBroker(cfg()["costs"])
    b.set_event(ev.append)
    assert b.create_order("Buy", "A", 1, 100.3).status == "Failed"        # 不合法檔位
    o = b.create_order("Buy", "A", 2, 100.0)
    b.on_quote("A", 100.5)
    assert o.status == "Submitted"
    b.on_quote("A", 99.9)
    assert o.status == "Filled" and o.fill_price == 99.9
    b.on_quote("A", 102.0)
    assert b.create_order("Sell", "A", 2).fill_price == 101.5           # 市價賣出：最新價少 1 檔
    assert b.get_position() == {}
    gross = (101.5 - 99.9) * 2000
    assert round(b.realized) == round(gross - twse.trade_cost(199_800, "buy", b.costs, True)
                                      - twse.trade_cost(203_000, "sell", b.costs, True))
    assert [(e.task, e.status) for e in ev] == [("NewOrder", "Failed"), ("NewOrder", "Success"), ("Deal", "Success"),
                                                ("NewOrder", "Success"), ("Deal", "Success")]
    o = b.create_order("Buy", "A", 1, 90.0)
    b.cancel_order(o.order_id)
    assert o.status == "Cancelled" and b.get_trades() == {}


# ── 觀察名單 ──
def test_watchlist():
    now = datetime(2026, 11, 4, 22, 30, tzinfo=timezone.utc)
    stock = dict(market="TW", stage=2, tt=[True] * 8, rs=90, avg_vol50=2_000_000, name="x", close=10)
    scan = {"generated_at": "2026-11-04T22:30:00+00:00", "stocks": [
        {**stock, "ticker": "2330.TW"}, {**stock, "ticker": "6488.TWO", "rs": 95},
        {**stock, "ticker": "1101.TW", "stage": 4}, {**stock, "ticker": "AAPL", "market": "US"},
        {**stock, "ticker": "2603.TW", "avg_vol50": 10_000}]}
    rows, warn = watchlist(scan, cfg(), today=now)
    assert [r["symbol"] for r in rows] == ["6488", "2330"] and warn == []
    rows, warn = watchlist(scan, cfg(), today=datetime(2026, 11, 6, 1, 0, tzinfo=timezone.utc))
    assert rows == [] and "不開新倉" in warn[0]


# ── 波段 ──
def swing_data(seed=1):
    """緩漲 320 天 → 窄幅整理 30 天（量縮）→ 帶量突破後上漲 → 回落。"""
    rng = np.random.default_rng(seed)
    up = 50 * np.exp(np.cumsum(rng.normal(0.0032, 0.012, 320)))
    base = up[-1] * (1 + rng.normal(0, 0.004, 30))
    top = max(up[-25:].max(), base.max())
    run = top * np.cumprod(np.r_[1.03, np.full(15, 1.015)])
    fall = run[-1] * np.cumprod(np.full(25, 0.985))
    close = np.r_[up, base, run, fall]
    vol = np.r_[np.full(320, 3e6), np.full(30, 1.5e6), np.full(16, 6e6), np.full(25, 4e6)]
    idx = pd.bdate_range("2024-01-01", periods=len(close))
    df = pd.DataFrame({"close": close, "volume": vol}, index=idx)
    df["open"] = df["close"].shift(1).fillna(df["close"])
    df["high"] = df[["open", "close"]].max(axis=1) * 1.004
    df["low"] = df[["open", "close"]].min(axis=1) * 0.996
    return df[["open", "high", "low", "close", "volume"]]


def test_swing_breakout_partial_and_exit():
    c = cfg(capital=5_000_000)
    res = swing.run({"A": swing_data()}, c)
    tr = res["trades"]
    assert len(tr) == 1, tr
    t = tr.iloc[0]
    assert t["entry_date"] == "2025-05-05"                     # 整理結束後的突破日（第 351 根）
    assert t["init_stop"] <= t["entry"] * 0.93 + 0.5
    assert t["reason"] in ("保本停損", "跌破 50 日線")
    assert t["pnl"] > 0                                        # 分批獲利 20% 後保本出場


def test_swing_odd_lot():
    pricey = swing_data()
    pricey[["open", "high", "low", "close"]] *= 15           # 股價約 1000 元以上
    lot = swing.run({"A": pricey}, cfg(capital=1_000_000))
    assert lot["trades"].empty and lot["skipped"].get("資金不足一張")
    odd = swing.run({"A": pricey}, cfg(capital=1_000_000, swing__lot_mode="odd"))
    t = odd["trades"].iloc[0]
    assert 0 < t["shares"] < 1000                             # 買零股
    risk = (t["entry"] - t["init_stop"]) * t["shares"]
    assert risk <= 1_000_000 * 0.01                           # 單筆風險不超過 1%
    assert t["shares"] * t["entry"] <= 1_000_000 * 0.25       # 單一個股不超過 25%


def test_backtest_cli_csv():
    from backtest import run as R
    d = Path(tempfile.mkdtemp())
    (d / "daily").mkdir()
    (d / "5m").mkdir()
    daily = swing_data()
    daily.index = pd.bdate_range(end="2026-11-03", periods=len(daily))   # 讓日 K 涵蓋分 K 的日期
    daily.iloc[-3:] = [100, 100.5, 99.5, 100, 3e6]                          # 昨收與分 K 價位一致
    daily.rename_axis("date").to_csv(d / "daily" / "A.csv")
    intr = pd.concat([day(OR + [("09:15", 100, 101.5, 100, 101.5, 400), ("09:20", 101.5, 104, 101, 103.5, 300),
                                ("09:25", 103.5, 108, 103, 107.5, 300)], date=x)
                      for x in ("2026-11-02", "2026-11-03")])
    pd.concat([day([("13:25", 100, 100, 100, 100, 1)], date="2026-10-30"), intr]).rename_axis("datetime").to_csv(d / "5m" / "A.csv")
    loose = ["--set", "universe.trend_template=false", "--set", "universe.stages=1,2,3,4",
             "--set", "universe.min_avg_volume_lots=0", "--set", "daytrade.or_volume_ratio=0"]
    R.main(["daytrade", "--source", "csv", "--csv-dir", str(d), "--symbols", "A", "--out", str(d / "out")] + loose)
    dt = next((d / "out").iterdir())
    trades = pd.read_csv(dt / "trades.csv")
    assert len(trades) == 2 and set(trades["reason"]) == {"資料結束"}, trades  # 最高 108 未達停利 108.5
    R.main(["swing", "--source", "csv", "--csv-dir", str(d), "--symbols", "A", "--out", str(d / "out"),
            "--set", "capital=5000000"])
    outs = list((d / "out").iterdir())
    assert len(outs) == 2 and all((o / "summary.txt").exists() for o in outs)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
