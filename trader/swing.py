"""波段模式：依《超級績效》規則的多檔投資組合回測（日 K）。

每天依序：
1. 前一天收盤跌破 50 日均線且量增的持股，今天開盤賣出。
2. 持股檢查停損（跳空跌破以開盤價成交）、分批獲利、保本停損上移。
3. 符合條件的候選股突破樞紐價就買進（依 RS 由高到低，受資金與檔數限制）。

條件判斷只用前一天收盤前的資料，避免偷看未來。
例外：盤中突破模式（intraday）用「當天全日成交量」檢查量增，實盤只能用預估量，回測結果會略為樂觀。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from . import twse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stage_scanner import stage_series  # noqa: E402


@dataclass
class Position:
    symbol: str
    entry_date: str
    entry: float
    shares: int
    stop: float
    init_stop: float
    partial_done: bool = False
    cost: float = 0.0          # 已發生成本
    realized: float = 0.0      # 分批賣出的已實現損益（未扣成本）
    exit_next_open: bool = False
    orig_shares: int = 0


def indicators(df: pd.DataFrame) -> pd.DataFrame:
    """欄位 open/high/low/close/volume → 加上判斷所需指標。"""
    c, v = df["close"], df["volume"]
    out = df.copy()
    s50, s150, s200 = (c.rolling(n).mean() for n in (50, 150, 200))
    out["s50"], out["s150"], out["s200"] = s50, s150, s200
    out["stage"], _ = stage_series(c, s50, s150, s200)
    hi52, lo52 = c.rolling(252, min_periods=200).max(), c.rolling(252, min_periods=200).min()
    tt = [
        (c > s150) & (c > s200), s150 > s200, s200 > s200.shift(21),
        (s50 > s150) & (s50 > s200), c > s50, c >= 1.3 * lo52, c >= 0.75 * hi52,
    ]
    out["tt7"] = np.logical_and.reduce(tt)
    out["rs_raw"] = (0.4 * c.pct_change(63) + 0.2 * c.pct_change(126)
                     + 0.2 * c.pct_change(189) + 0.2 * c.pct_change(252))
    r = c.pct_change()
    out["contraction"] = r.rolling(10).std() / r.rolling(50).std()
    out["vol_ratio"] = v.rolling(10).mean() / v.rolling(50).mean()
    out["avg_vol50"] = v.rolling(50).mean()
    return out


def run(data: dict[str, pd.DataFrame], cfg: dict, start: str | None = None) -> dict:
    sw, uni, rk, costs = cfg["swing"], cfg["universe"], cfg["risk"], cfg["costs"]
    ind = {s: indicators(df) for s, df in data.items()}
    for s, d in ind.items():
        d["pivot"] = d["high"].rolling(sw["pivot_lookback"]).max().shift(1)

    # RS：每天在這批股票內排名，轉成 1～99
    rs_raw = pd.DataFrame({s: d["rs_raw"] for s, d in ind.items()})
    use_rs = rs_raw.shape[1] >= 10
    rs = (1 + rs_raw.rank(axis=1, pct=True) * 98).round() if use_rs else None

    dates = sorted(set().union(*[d.index for d in ind.values()]))
    if start:
        dates = [d for d in dates if d >= pd.Timestamp(start)]

    cash = float(cfg["capital"])
    pos: dict[str, Position] = {}
    trades, equity = [], []
    skipped: dict[str, int] = {}

    def bar(s, d):
        df = ind[s]
        return df.loc[d] if d in df.index else None

    def prev(s, d):
        df = ind[s]
        i = df.index.searchsorted(d) - 1
        return df.iloc[i] if i >= 0 else None

    unit = 1 if sw["lot_mode"] == "odd" else 1000   # 零股模式以「股」為單位

    def fee(px, shares, side):
        # 零股模式：整張部分下整股單、不足一張部分下盤中零股單，兩筆各自計費
        lot_sh, odd_sh = (shares // 1000) * 1000, shares % 1000
        return sum(twse.trade_cost(px * n, side, costs, False) for n in (lot_sh, odd_sh) if n)

    def sell(p: Position, d, px, shares, reason):
        nonlocal cash
        amt = px * shares
        cost = fee(px, shares, "sell")
        cash += amt - cost
        p.cost += cost
        p.realized += (px - p.entry) * shares
        p.shares -= shares
        if p.shares == 0:
            pnl = p.realized - p.cost
            risk = (p.entry - p.init_stop) * p.orig_shares
            trades.append({"symbol": p.symbol, "entry_date": p.entry_date, "entry": p.entry,
                           "shares": p.orig_shares, "exit_date": d.strftime("%Y-%m-%d"), "exit": px,
                           "reason": reason, "pnl": round(pnl, 0), "init_stop": p.init_stop,
                           "r": round(pnl / risk, 2) if risk > 0 else None})
            del pos[p.symbol]

    for d in dates:
        # 1～2. 持股管理
        for s in list(pos):
            p, b = pos[s], bar(s, d)
            if b is None:
                continue
            o, h, l, c = b["open"], b["high"], b["low"], b["close"]
            if p.exit_next_open:
                sell(p, d, twse.ticks_away(o, -costs["slippage_ticks"]), p.shares, "跌破 50 日線")
                continue
            if l <= p.stop:
                px = twse.ticks_away(min(o, p.stop), -costs["slippage_ticks"])
                sell(p, d, px, p.shares, "保本停損" if p.stop >= p.entry else "停損")
                continue
            if sw["partial_take_pct"] is not None and not p.partial_done:
                tgt = twse.round_to_tick(p.entry * (1 + sw["partial_take_pct"] / 100), "up")
                if h >= tgt:
                    n = int(p.shares // unit * sw["partial_take_size"]) * unit
                    if n > 0:
                        sell(p, d, max(o, tgt), n, "分批獲利")
                    p.partial_done = True
                    if s not in pos:
                        continue
            if h >= p.entry + sw["breakeven_r"] * (p.entry - p.init_stop):
                p.stop = max(p.stop, p.entry)
            if (c < b["s50"]) and b["volume"] >= b["avg_vol50"] * sw["ma_break_volume_ratio"]:
                p.exit_next_open = True

        # 3. 進場
        cands = []
        for s in ind:
            if s in pos:
                continue
            b, pv = bar(s, d), prev(s, d)
            if b is None or pv is None or pd.isna(pv["pivot"]) or pd.isna(pv["stage"]):
                continue
            if int(pv["stage"]) not in uni["stages"]:
                continue
            if uni["trend_template"] and not pv["tt7"]:
                continue
            r = rs.loc[pv.name, s] if use_rs and pv.name in rs.index else None
            if use_rs and uni["trend_template"] and (r is None or pd.isna(r) or r < 70):
                continue
            if use_rs and (r is None or pd.isna(r) or r < uni["min_rs"]):
                continue
            if pv["avg_vol50"] < uni["min_avg_volume_lots"] * 1000:
                continue
            if not (pv["contraction"] <= sw["max_contraction"] and pv["vol_ratio"] <= sw["max_vol_ratio"]):
                continue
            pivot = b["pivot"]
            trig = twse.ticks_away(pivot, 1)
            if sw["entry_timing"] == "intraday":
                if b["high"] < trig or b["volume"] < pv["avg_vol50"] * sw["breakout_volume_ratio"]:
                    continue
                px = twse.ticks_away(max(b["open"], trig), costs["slippage_ticks"])
            else:  # next_open：前一天收盤突破、量增，今天開盤買
                if not (pv["close"] > pv["pivot"] and pv["volume"] >= prev_avg(ind[s], pv.name) * sw["breakout_volume_ratio"]):
                    continue
                pivot = pv["pivot"]
                px = twse.ticks_away(b["open"], costs["slippage_ticks"])
            if px > pivot * (1 + sw["max_chase_pct"] / 100):
                skipped["追高超過上限"] = skipped.get("追高超過上限", 0) + 1
                continue
            cands.append((r if r is not None else 0, s, px))

        for _, s, px in sorted(cands, reverse=True):
            if len(pos) >= rk["max_positions"]:
                skipped["已達持股檔數上限"] = skipped.get("已達持股檔數上限", 0) + 1
                break
            eq = cash + sum(p.shares * float(bar(q, d)["close"] if bar(q, d) is not None else p.entry)
                            for q, p in pos.items())
            stop = twse.round_to_tick(px * (1 - sw["stop_loss_pct"] / 100), "up")
            risk_units = eq * rk["risk_per_trade_pct"] / 100 / ((px - stop) * unit)
            cap_units = eq * sw["max_position_pct"] / 100 / (px * unit)
            n = int(min(risk_units, cap_units))
            while n >= 1 and px * n * unit + fee(px, n * unit, "buy") > cash:
                n -= 1
            if n < 1:
                reason = "資金不足一股" if unit == 1 else "資金不足一張"
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            shares = n * unit
            f = fee(px, shares, "buy")
            cash -= px * shares + f
            pos[s] = Position(s, d.strftime("%Y-%m-%d"), px, shares, stop, stop, cost=f, orig_shares=shares)

        mv = sum(p.shares * float(bar(q, d)["close"]) for q, p in pos.items() if bar(q, d) is not None)
        equity.append((d, cash + mv))

    # 期末未平倉：以最後收盤價計算（標示為未平倉）
    open_pos = [{"symbol": s, "entry_date": p.entry_date, "entry": p.entry, "shares": p.shares,
                 "stop": p.stop, "last": float(ind[s]["close"].iloc[-1])} for s, p in pos.items()]
    eq = pd.Series(dict(equity), name="equity")
    return {"trades": pd.DataFrame(trades), "equity": eq, "open": open_pos,
            "skipped": skipped, "rs_used": use_rs}


def prev_avg(df: pd.DataFrame, d) -> float:
    """d 那天之前的 50 日均量（不含 d 當天）。"""
    i = df.index.get_loc(d)
    return float(df["volume"].iloc[max(0, i - 50):i].mean()) if i > 0 else float("nan")
