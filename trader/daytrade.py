"""現股當沖：開盤區間突破（Opening Range Breakout）。

輸入一天內多檔股票的分 K（1 或 5 分鐘），依時間順序逐根處理，
回傳當天的成交紀錄。回測與程式內模擬使用同一套邏輯。

成交假設（回測用，保守估計）：
- 進場：K 棒最高價（做空為最低價）越過觸發價即成交，價格為「觸發價與開盤價較不利者」再加滑價。
- 進場那根 K 棒不檢查停損停利（無法得知 K 棒內先後順序），從下一根開始。
- 同一根 K 棒同時碰到停損與停利，視為先停損。
- 強制平倉：時間到的第一根 K 棒以開盤價（加滑價）平倉。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from . import twse


@dataclass
class Trade:
    date: str
    symbol: str
    direction: str
    lots: int
    entry_time: str
    entry: float
    stop: float
    target: float | None
    exit_time: str = ""
    exit: float = 0.0
    reason: str = ""
    pnl: float = 0.0          # 扣除成本後損益（元）
    cost: float = 0.0
    risk: float = 0.0         # 進場時的風險金額（元）

    @property
    def r(self) -> float:
        return self.pnl / self.risk if self.risk else 0.0


@dataclass
class _SymState:
    prev_close: float
    avg_vol50: float | None
    or_high: float = 0.0
    or_low: float = float("inf")
    or_vol: float = 0.0
    entries: int = 0
    skip: str = ""            # 不交易的原因
    pos: Trade | None = None


def _hhmm(ts: pd.Timestamp) -> str:
    return ts.strftime("%H:%M")


def _add(t: str, minutes: int) -> str:
    h, m = map(int, t.split(":"))
    m += h * 60 + minutes
    return f"{m // 60:02d}:{m % 60:02d}"


def run_day(bars: dict[str, pd.DataFrame], info: dict[str, dict], cfg: dict) -> tuple[list[Trade], dict]:
    """
    bars：{symbol: 當天分 K DataFrame（index 為 K 棒開始時間，欄位 open/high/low/close/volume，volume 單位為股）}
    info：{symbol: {"prev_close": 昨收, "avg_vol50": 50 日均量（股，可為 None）}}
    回傳 (成交紀錄, 當天統計)
    """
    dt, rk, costs = cfg["daytrade"], cfg["risk"], cfg["costs"]
    long = dt["direction"] == "long"
    or_end = _add("09:00", dt["opening_range_minutes"])
    start, end, force = dt["entry"]["start"], dt["entry"]["end"], dt["exit"]["force_close"]
    slip = costs["slippage_ticks"]
    capital = cfg["capital"]
    max_loss = capital * rk["max_daily_loss_pct"] / 100

    st = {s: _SymState(prev_close=info[s]["prev_close"], avg_vol50=info[s].get("avg_vol50")) for s in bars}
    rows = []
    for s, df in bars.items():
        for ts, b in df.iterrows():
            rows.append((ts, s, b))
    rows.sort(key=lambda x: (x[0], x[1]))

    trades: list[Trade] = []
    realized, orders, stopped = 0.0, 0, False
    stats = {"signals": 0, "skipped": {}}

    seen = set()

    def skip(reason):
        # 每檔每個原因只計一次
        if (sym, reason) not in seen:
            seen.add((sym, reason))
            stats["skipped"][reason] = stats["skipped"].get(reason, 0) + 1

    def close(sym, ts, px, reason):
        nonlocal realized, orders
        p = st[sym].pos
        p.exit_time, p.exit, p.reason = _hhmm(ts), px, reason
        amt_in, amt_out = p.entry * p.lots * 1000, px * p.lots * 1000
        if long:
            gross = amt_out - amt_in
            cost = twse.trade_cost(amt_in, "buy", costs, True) + twse.trade_cost(amt_out, "sell", costs, True)
        else:
            gross = amt_in - amt_out
            cost = twse.trade_cost(amt_in, "sell", costs, True) + twse.trade_cost(amt_out, "buy", costs, True)
        p.cost, p.pnl = cost, gross - cost
        realized += p.pnl
        orders += 1
        trades.append(p)
        st[sym].pos = None

    def unrealized(ts):
        u = 0.0
        for s2, x in st.items():
            if x.pos is not None:
                last = cur_close.get(s2, x.pos.entry)
                u += (last - x.pos.entry if long else x.pos.entry - last) * x.pos.lots * 1000
        return u

    cur_close: dict[str, float] = {}
    for ts, sym, b in rows:
        x, t = st[sym], _hhmm(ts)
        o, h, l, c, v = (float(b[k]) for k in ("open", "high", "low", "close", "volume"))
        cur_close[sym] = c
        lim_up, lim_dn = twse.limit_prices(x.prev_close)

        # 1. 開盤區間
        if t < or_end:
            x.or_high, x.or_low, x.or_vol = max(x.or_high, h), min(x.or_low, l), x.or_vol + v
            continue

        # 2. 持倉管理
        if x.pos is not None:
            p = x.pos
            if t >= force:
                px = twse.ticks_away(o, -slip if long else slip)
                close(sym, ts, min(max(px, lim_dn), lim_up), "強制平倉")
                continue
            if long:
                if l <= p.stop:
                    close(sym, ts, max(twse.ticks_away(min(o, p.stop), -slip), lim_dn), "停損")
                elif p.target is not None and h >= p.target:
                    close(sym, ts, max(o, p.target), "停利")
            else:
                if h >= p.stop:
                    close(sym, ts, min(twse.ticks_away(max(o, p.stop), slip), lim_up), "停損")
                elif p.target is not None and l <= p.target:
                    close(sym, ts, min(o, p.target), "停利")
            continue

        # 3. 進場
        if t >= end or t < start or x.entries >= dt["max_entries_per_symbol"] or x.skip:
            continue
        if not x.or_high or x.or_low == float("inf"):
            x.skip = "沒有開盤區間資料"
            continue
        width = (x.or_high - x.or_low) / x.or_low * 100
        if not dt["min_range_pct"] <= width <= dt["max_range_pct"]:
            x.skip = "開盤區間寬度不符"
            skip(x.skip)
            continue
        if dt["or_volume_ratio"] > 0:
            if not x.avg_vol50:
                x.skip = "沒有均量資料"
                skip(x.skip)
                continue
            if x.or_vol < x.avg_vol50 * dt["or_volume_ratio"]:
                x.skip = "開盤區間量能不足"
                skip(x.skip)
                continue

        trigger = twse.ticks_away(x.or_high, 1) if long else twse.ticks_away(x.or_low, -1)
        if (long and h < trigger) or (not long and l > trigger):
            continue
        stats["signals"] += 1
        entry = twse.ticks_away(max(o, trigger), slip) if long else twse.ticks_away(min(o, trigger), -slip)
        buf = dt["limit_buffer_pct"] / 100
        if (long and entry >= lim_up * (1 - buf)) or (not long and entry <= lim_dn * (1 + buf)):
            skip("接近漲跌停")
            x.entries += 1
            continue
        if stopped or realized + unrealized(ts) <= -max_loss:
            stopped = True
            skip("已達每日虧損上限")
            continue
        open_pos = sum(1 for y in st.values() if y.pos is not None)
        if open_pos >= rk["max_positions"]:
            skip("已達同時持有上限")
            continue
        if orders + 2 > rk["max_orders_per_day"]:
            skip("已達每日下單筆數上限")
            continue

        sl = dt["exit"]["stop_loss_pct"] / 100
        if long:
            stop = max(x.or_low, twse.round_to_tick(entry * (1 - sl), "up"))
        else:
            stop = min(x.or_high, twse.round_to_tick(entry * (1 + sl), "down"))
        risk_per_lot = abs(entry - stop) * 1000
        if risk_per_lot <= 0:
            skip("停損距離為 0")
            continue
        lots = int(min(dt["max_amount_per_trade"] / (entry * 1000),
                       capital * rk["risk_per_trade_pct"] / 100 / risk_per_lot))
        if lots < 1:
            skip("資金不足一張")
            x.entries += 1
            continue
        tp = dt["exit"]["take_profit_pct"]
        target = None if tp is None else (twse.round_to_tick(entry * (1 + tp / 100), "up") if long
                                          else twse.round_to_tick(entry * (1 - tp / 100), "down"))
        x.pos = Trade(date=ts.strftime("%Y-%m-%d"), symbol=sym, direction=dt["direction"], lots=lots,
                      entry_time=t, entry=entry, stop=stop, target=target, risk=risk_per_lot * lots)
        x.entries += 1
        orders += 1

    # 資料結束仍有部位（例如最後一根 K 棒早於強制平倉時間）：以最後收盤價平倉
    for sym, x in st.items():
        if x.pos is not None:
            last_ts = bars[sym].index[-1]
            close(sym, last_ts, float(bars[sym]["close"].iloc[-1]), "資料結束")

    stats["pnl"] = realized
    return trades, stats
