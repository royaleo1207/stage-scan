"""程式內模擬下單（broker: paper）。

介面比照凱基 SuperPy 的 api.Order：create_order／cancel_order／get_trades／get_position，
事件以回呼通知（task：NewOrder、CancelOrder、Deal；status：Success、Failed），
之後換成 kgi_sim／kgi_live 時，交易程式不用改。

成交規則：
- 市價單：以「最新價 ± 滑價」立即成交；還沒有報價時，等第一筆報價成交。
- 限價單：買單在報價 ≤ 限價時、賣單在報價 ≥ 限價時，以限價成交（若報價更有利，以報價成交）。
- 不模擬部分成交、排隊順序與漲跌停鎖死，結果會比實際樂觀。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from . import twse

MKT = "MKT"


@dataclass
class PaperOrder:
    order_id: str
    action: str             # Buy / Sell
    symbol: str
    qty: int                # 張
    price: float | str      # 限價或 MKT
    order_cond: str         # CASH / CASH_SELLING
    name: str
    status: str = "Submitted"   # Submitted / Filled / Cancelled
    fill_price: float | None = None
    ts: str = ""


@dataclass
class PaperEvent:
    task: str
    status: str
    order_id: str
    symbol: str
    action: str
    quantity: int
    price: float | str | None
    msg: str = ""
    ts: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


class PaperBroker:
    def __init__(self, costs: dict, day_trade: bool = True, clock: Callable[[], str] | None = None):
        self.costs, self.day_trade = costs, day_trade
        self._ids = itertools.count(1)
        self._cb: Callable[[PaperEvent], None] = lambda e: None
        self._clock = clock or (lambda: datetime.now().isoformat(timespec="seconds"))
        self.orders: dict[str, PaperOrder] = {}
        self.positions: dict[str, dict] = {}    # symbol → {"qty": 張（負數為空單）, "avg": 均價}
        self.realized = 0.0
        self.last: dict[str, float] = {}

    # ── 與 SuperPy 對應的介面 ──
    def set_event(self, cb: Callable[[PaperEvent], None]) -> None:
        self._cb = cb

    def create_order(self, action: str, symbol: str, qty: int, price: float | str = MKT,
                     order_cond: str = "CASH", name: str = "") -> PaperOrder:
        oid = f"P{next(self._ids):05d}"
        bad = None
        if action not in ("Buy", "Sell"):
            bad = f"不認得的買賣別：{action}"
        elif not isinstance(qty, int) or qty < 1:
            bad = "張數需為正整數"
        elif price != MKT and (not isinstance(price, (int, float)) or twse.round_to_tick(price) != price):
            bad = f"價格不在合法檔位：{price}"
        o = PaperOrder(oid, action, symbol, qty, price, order_cond, name, ts=self._clock())
        if bad:
            o.status = "Failed"
            self._cb(PaperEvent("NewOrder", "Failed", oid, symbol, action, qty, price, bad, self._clock()))
            return o
        self.orders[oid] = o
        self._cb(PaperEvent("NewOrder", "Success", oid, symbol, action, qty, price, ts=self._clock()))
        if symbol in self.last:
            self._try_fill(o, self.last[symbol])
        return o

    def cancel_order(self, order_id: str) -> None:
        o = self.orders.get(order_id)
        if o is None or o.status != "Submitted":
            self._cb(PaperEvent("CancelOrder", "Failed", order_id, o.symbol if o else "", "", 0, None,
                                "委託不存在或已成交", self._clock()))
            return
        o.status = "Cancelled"
        self._cb(PaperEvent("CancelOrder", "Success", order_id, o.symbol, o.action, o.qty, o.price, ts=self._clock()))

    def cancel_order_all(self) -> None:
        for oid, o in list(self.orders.items()):
            if o.status == "Submitted":
                self.cancel_order(oid)

    def get_trades(self, full: bool = False) -> dict[str, PaperOrder]:
        return {k: o for k, o in self.orders.items() if full or o.status == "Submitted"}

    def get_position(self) -> dict[str, dict]:
        return {s: dict(p) for s, p in self.positions.items() if p["qty"] != 0}

    # ── 報價輸入 ──
    def on_quote(self, symbol: str, price: float) -> None:
        self.last[symbol] = price
        for o in list(self.orders.values()):
            if o.symbol == symbol and o.status == "Submitted":
                self._try_fill(o, price)

    def _try_fill(self, o: PaperOrder, last: float) -> None:
        buy = o.action == "Buy"
        if o.price == MKT:
            px = twse.ticks_away(last, self.costs["slippage_ticks"] * (1 if buy else -1))
        elif (buy and last <= o.price) or (not buy and last >= o.price):
            px = min(o.price, last) if buy else max(o.price, last)
        else:
            return
        o.status, o.fill_price = "Filled", px
        self._book(o.symbol, o.qty if buy else -o.qty, px)
        self._cb(PaperEvent("Deal", "Success", o.order_id, o.symbol, o.action, o.qty, px, ts=self._clock()))

    def _book(self, symbol: str, dq: int, px: float) -> None:
        amt = abs(dq) * px * 1000
        self.realized -= twse.trade_cost(amt, "buy" if dq > 0 else "sell", self.costs, self.day_trade)
        p = self.positions.setdefault(symbol, {"qty": 0, "avg": 0.0})
        q0 = p["qty"]
        if q0 == 0 or (q0 > 0) == (dq > 0):          # 新倉或加碼
            p["avg"] = (p["avg"] * abs(q0) + px * abs(dq)) / (abs(q0) + abs(dq))
            p["qty"] = q0 + dq
            return
        closed = min(abs(q0), abs(dq))                 # 平倉（可能反手）
        self.realized += (px - p["avg"]) * closed * 1000 * (1 if q0 > 0 else -1)
        p["qty"] = q0 + dq
        if p["qty"] == 0:
            p["avg"] = 0.0
        elif (p["qty"] > 0) != (q0 > 0):
            p["avg"] = px
