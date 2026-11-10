"""下單引擎：收計畫 → 下單前檢查 → 送單 → 成交建立部位 → 依出場規則監控 → 送平倉單。

所有狀態存在 Store（trader/runtime/state.json），重啟後接續監控。
SuperPy 回呼與行情來自其他執行緒，所有對外方法都先取得 self.lock。
"""

from __future__ import annotations

import threading
import time as _time
from datetime import date, datetime, timedelta
from typing import Any, Callable

from . import exit_rules as xr
from . import market_hours as mh
from . import twse
from .kgi_spec import (CATEGORY_LABEL, OrderSpec, close_method, exit_unit, is_closing, position_side,
                       shares_of, validate_spec)
from .risk import RiskLimits, RiskManager
from .store import Store

ACTIVE = ("armed", "sending", "submitted", "partial")

DEFAULT_CFG = {
    "risk": RiskLimits().__dict__,
    "exits": {
        "tw_common_price": "limit_updown",   # limit_updown（以跌停賣出／漲停買回，ROD）或 market（市價 IOC）
        "tw_odd_slip_ticks": 5,              # 零股出場：以最新價往不利方向 N 檔的限價
        "us_slip_pct": 0.5,                  # 美股出場：以最新價往不利方向 X% 的限價（API 只接受限價）
        "retry_sec": 20,                     # 出場單多久沒成交就刪單重掛
        "max_retries": 5,
    },
    "enforce_hours_in_simulation": False,    # 凱基模擬環境營業日 08:30～隔日 05:00 都可下單，預設不檢查時段
    "costs": {"fee_rate": 0.001425, "fee_discount": 1.0, "min_fee": 20, "tax_rate": 0.003,
              "tax_day_trade": 0.0015, "slippage_ticks": 1},
}


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


class Engine:
    def __init__(self, broker: Any, store: Store, cfg: dict | None = None, live: bool = False,
                 clock: Callable[[], datetime] | None = None, holidays: set[date] | None = None):
        self.broker, self.store = broker, store
        self.cfg = _deep_merge(DEFAULT_CFG, cfg or {})
        self.live = live
        self.dry_run = bool(getattr(broker, "dry_run", False)) and not live   # 演練：正式登入、本機成交
        self.now = clock or mh.now_tpe
        self.holidays = holidays
        self.risk = RiskManager(RiskLimits.from_cfg(self.cfg["risk"]))
        self.lock = threading.RLock()
        self.last: dict[str, float] = {}          # "TW:2330:0" → 最新價
        broker.set_handlers(self.on_broker_event, self.on_tick)

    # ── 小工具 ──
    @property
    def d(self) -> dict:
        return self.store.data

    @property
    def mode(self) -> str:
        return "live" if self.live else ("dry_run" if self.dry_run else "simulation")

    def log(self, kind: str, msg: str, **extra) -> None:
        self.store.log(kind, msg, **extra)

    def risk_day(self) -> str:
        # 以台北時間早上 6 點為一天的分界，讓美股夜盤（21:30～隔日 05:00）算在同一天
        return (self.now().astimezone(mh.TPE) - timedelta(hours=6)).date().isoformat()

    def day(self) -> dict:
        return self.d["days"].setdefault(self.risk_day(), {"amount": 0.0, "orders": 0, "realized": 0.0,
                                                           "halted": False, "halt_reason": ""})

    @staticmethod
    def qkey(market: str, symbol: str, odd: bool) -> str:
        return f"{market}:{symbol}:{int(odd)}"

    def _check_hours(self) -> bool:
        return self.live or self.dry_run or self.cfg["enforce_hours_in_simulation"]

    def _can_order_now(self, spec: OrderSpec) -> tuple[bool, str]:
        if spec.market == "TW":
            return mh.tw_can_order(spec.unit, self.now(), self.holidays)
        return True, ""

    # ── 計畫 ──
    def submit_plan(self, plan: dict) -> dict:
        with self.lock:
            exp = plan.get("expected_mode")
            if exp and exp != self.mode:
                nm = {"live": "正式", "simulation": "模擬", "dry_run": "演練"}
                return {"ok": False, "error": f"網頁確認的是「{nm.get(exp, exp)}」模式，"
                                              f"下單程式目前是「{nm[self.mode]}」模式，整份計畫未執行"}
            items = plan.get("items")
            if not isinstance(items, list) or not items:
                return {"ok": False, "error": "計畫沒有任何委託"}
            if len(items) > 100:
                return {"ok": False, "error": "一次最多 100 筆"}
            self.log("plan", f"收到計畫 {len(items)} 筆", plan_id=plan.get("id"))
            results = []
            for it in items:
                try:
                    if it.get("kind") == "position_exit":
                        r = self._attach_exit(it)
                    else:
                        r = self._add_order_item(it)
                except (ValueError, KeyError, TypeError) as e:
                    r = {"ok": False, "reason": f"格式錯誤：{e}"}
                r["id"] = it.get("id")
                r["symbol"] = it.get("symbol")
                results.append(r)
                self.log("plan_item", ("接受" if r["ok"] else "拒絕") + f" {it.get('symbol')}：{r.get('reason', '')}",
                         item=it, result=r)
            self.store.save()
            return {"ok": True, "mode": self.mode, "results": results}

    def _add_order_item(self, it: dict) -> dict:
        spec = OrderSpec.from_dict(it)
        errs = validate_spec(spec)
        if errs:
            return {"ok": False, "reason": "；".join(errs)}
        rules = xr.ExitRules.from_dict(it.get("exit"))
        side = position_side(spec.action, spec.category) if spec.market == "TW" else (
            "long" if spec.action == "Buy" else None)
        if rules and not side:
            return {"ok": False, "reason": "出場規則只能加在開倉單（買進現股／融資、融券賣出、借券賣出、現股當沖先賣、美股買進）"}
        if spec.category == "daytrade_short":
            rules = rules or xr.ExitRules(position_type="daytrade")
            if rules.position_type != "daytrade":
                return {"ok": False, "reason": "現股當沖先賣必須當日買回，部位類型需為當沖"}
        reason = self._precheck(spec, rules, side)
        if reason:
            return {"ok": False, "reason": reason}
        oid = self.store.next_id("E" if side else "C")
        o = {"id": oid, "market": spec.market, "spec": spec.to_dict(), "role": "entry" if side else "close",
             "side": side, "rules": rules.to_dict() if rules else None, "status": "armed", "filled": 0,
             "avg": None, "created": self.now().isoformat(timespec="seconds"), "msg": "", "est": self._est(spec),
             "trade_date": mh.trade_date(spec.market, self.now()).isoformat(), "plan_item": it.get("id")}
        self.d["orders"][oid] = o
        if rules and rules.has_entry_condition():
            self.broker.subscribe(spec.market, spec.symbol, spec.unit == "odd")
            o["msg"] = "等待進場條件（時間窗／突破價）"
            self.log("armed", f"{oid} {spec.symbol} 等待進場條件", order=oid)
            return {"ok": True, "local_id": oid, "reason": o["msg"]}
        return self._send(o)

    def _attach_exit(self, it: dict) -> dict:
        """對既有庫存設定停損停利（不送進場單）。"""
        market = str(it.get("market", "")).upper()
        sym = str(it.get("symbol", "")).strip().upper()
        pos = it.get("position") or {}
        kind = pos.get("kind", "cash")
        unit = "shares" if market == "US" else ("odd" if kind == "odd" else "common")
        kind = "cash" if kind == "odd" else kind
        if kind not in ("cash", "margin", "short", "lend_sell", "lend"):
            return {"ok": False, "reason": f"不支援的庫存種類：{kind}"}
        kind = "lend_sell" if kind == "lend" else kind
        side = "short" if kind in ("short", "lend_sell") else "long"
        shares = int(pos.get("shares") or 0)
        if shares <= 0:
            return {"ok": False, "reason": "庫存股數需大於 0"}
        rules = xr.ExitRules.from_dict(it.get("exit"))
        if not rules:
            return {"ok": False, "reason": "請至少設定一項出場規則"}
        avail = self._inventory_shares(market, sym, {"cash": "cash", "margin": "margin", "short": "short",
                                                    "lend_sell": "lend"}[kind], odd=unit == "odd")
        if avail is not None and avail < shares:
            return {"ok": False, "reason": f"庫存只有 {avail} 股，少於設定的 {shares} 股"}
        dup = [p for p in self.d["positions"].values()
               if p["status"] in ("open", "exiting") and p["market"] == market and p["symbol"] == sym and p["kind"] == kind]
        if dup:
            return {"ok": False, "reason": f"這檔庫存已由 {dup[0]['id']} 監控中，請先在狀態頁停止監控"}
        avg = _f(pos.get("avg")) or _f(pos.get("last"))
        if not avg:
            return {"ok": False, "reason": "缺少成本價（以 % 設定停損停利時需要），請填成本價"}
        pid = self.store.next_id("P")
        self.d["positions"][pid] = self._new_pos(pid, market, sym, it.get("name", ""), side, kind,
                                                 unit, unit, shares, avg, rules, source="inventory",
                                                 currency=pos.get("currency", "MUT"))
        self.broker.subscribe(market, sym, unit == "odd")
        self.log("attach", f"{pid} {sym} 庫存 {shares} 股開始監控出場規則", position=pid)
        return {"ok": True, "local_id": pid, "reason": "已開始監控"}

    # ── 下單前檢查 ──
    def _est_price(self, spec: OrderSpec, c: dict | None) -> float | None:
        last = self.last.get(self.qkey(spec.market, spec.symbol, spec.unit == "odd"))
        if spec.price_type == "LMT":
            return float(spec.price)
        c = c or {}
        if spec.price_type == "LIMIT_UP" or (spec.price_type == "MKT" and spec.action == "Buy"):
            return c.get("bull") or last or c.get("ref")
        if spec.price_type == "LIMIT_DOWN":
            return c.get("bear") or last or c.get("ref")
        if spec.price_type == "MKT":
            return last or c.get("ref") or c.get("bear")
        return c.get("ref") or last

    def _est(self, spec: OrderSpec, c: dict | None = None) -> float:
        p = self._est_price(spec, c) or 0.0
        return round(p * shares_of(spec), 2)

    def _inventory_shares(self, market: str, symbol: str, kind: str, odd: bool = False) -> int | None:
        try:
            inv = self.broker.tw_inventory() if market == "TW" else self.broker.us_inventory()
        except Exception as e:  # noqa: BLE001
            self.log("error", f"查詢庫存失敗：{e}")
            return None
        rows = [r for r in inv if r["symbol"] == symbol]
        if kind == "cash" and market == "TW":
            q = sum(r["shares"] for r in rows if r["kind"] == "cash")
            if odd:
                q += sum(r["shares"] for r in rows if r["kind"] == "odd")
            return q
        return sum(r["shares"] for r in rows if r["kind"] == kind)

    def _reserved_close(self, market: str, symbol: str, action: str, category: str) -> int:
        n = 0
        for o in self.d["orders"].values():
            s = o["spec"]
            if o["status"] in ACTIVE and o["market"] == market and s["symbol"] == symbol and \
                    s["action"] == action and s["category"] == category and o["role"] in ("close", "exit"):
                n += shares_of(s["unit"], max(0, s["qty"] - o["filled"]))
        return n

    def short_exposure(self) -> float:
        tot = 0.0
        for p in self.d["positions"].values():
            if p["status"] in ("open", "exiting") and p["side"] == "short":
                px = self.last.get(self.qkey(p["market"], p["symbol"], p["unit"] == "odd")) or p["avg"]
                tot += self.risk.to_twd(p["market"], px * p["shares"])
        for o in self.d["orders"].values():
            if o["status"] in ACTIVE and o["side"] == "short" and o["role"] == "entry":
                tot += self.risk.to_twd(o["market"], o["est"] * (1 - o["filled"] / max(1, o["spec"]["qty"])))
        return tot

    def _precheck(self, spec: OrderSpec, rules: xr.ExitRules | None, side: str | None,
                  for_send: bool = False) -> str | None:
        now = self.now()
        daytrade = bool(rules and rules.position_type == "daytrade")
        if spec.market == "TW":
            if not self.broker.has_tw:
                return "下單程式未設定證券帳號（KGI_TW_ACCOUNT）"
            c = self.broker.tw_contract(spec.symbol)
            if c is None:
                return f"查無台股商品 {spec.symbol}（Order.contract）"
            if spec.price_type == "LMT":
                p = float(spec.price)
                if twse.round_to_tick(p) != round(p, 4):
                    return f"價格 {p:g} 不在合法檔位（{twse.tick_size(p):g} 元一檔）"
                if c.get("bull") and c.get("bear") and not (c["bear"] <= p <= c["bull"]):
                    return f"價格 {p:g} 超出漲跌停範圍 {c['bear']:g}～{c['bull']:g}"
            if daytrade:
                if spec.unit != "common":
                    return "當沖只能用盤中整股（零股與盤後交易不能當沖）"
                dt = c.get("day_trade")
                if spec.category == "daytrade_short" and dt != "Yes":
                    return f"{spec.symbol} 不可先賣後買當沖（Contracts 的 day_trade = {dt}）"
                if spec.category == "cash" and dt not in ("Yes", "OnlyBuy"):
                    return f"{spec.symbol} 不可現股當沖（Contracts 的 day_trade = {dt}）"
                dl = xr.flatten_deadline(rules, "TW", mh.tw_trade_date(now), now, self.holidays)
                if dl and now >= dl:
                    return f"已過當沖平倉時間（{dl:%H:%M}），不再開新的當沖部位"
            if spec.category in ("margin", "short") and side:
                cr = self.broker.tw_credit(spec.symbol)
                if not cr["ok"]:
                    return f"信用額度查詢失敗：{cr.get('msg') or '無回應'}"
                field, label = ("margin_qty", "融資") if spec.category == "margin" else ("short_qty", "融券")
                avail = cr.get(field)
                if avail is not None and avail < spec.qty:
                    return f"{label}可用額度 {avail:g} 張，不足 {spec.qty} 張（TSECreditInfo）"
            if spec.category == "lend_sell":
                have = self._inventory_shares("TW", spec.symbol, "lend")
                if have is not None and have < shares_of(spec):
                    return f"借券餘額 {have} 股，不足借券賣出 {shares_of(spec)} 股"
            if is_closing(spec.action, spec.category):
                kind = {"cash": "cash", "margin": "margin", "short": "short"}[spec.category]
                have = self._inventory_shares("TW", spec.symbol, kind, odd=spec.unit in ("odd", "odd_after"))
                need = shares_of(spec) + self._reserved_close("TW", spec.symbol, spec.action, spec.category)
                if have is not None and have < need:
                    what = {"cash": "現股", "margin": "融資", "short": "融券"}[kind]
                    return f"{what}庫存 {have} 股，不足這筆 {shares_of(spec)} 股（含其他未成交平倉單）"
            if self._check_hours():
                ok, why = self._can_order_now(spec)
                if not ok and not (rules and rules.has_entry_condition() and not for_send):
                    return why
        else:
            if not self.broker.has_us:
                return "下單程式未設定複委託帳號（KGI_US_ACCOUNT）"
            c = self.broker.us_contract(spec.symbol)
            if c is None:
                return f"查無美股商品 {spec.symbol}（SubOrder.contract）"
            if c.get("market") not in (None, "US", "Market.US"):
                return f"{spec.symbol} 不是美國市場商品，SuperPy 複委託目前只支援美股"
            if spec.qty % max(1, c.get("board_lot") or 1):
                return f"股數需為 {c['board_lot']} 的倍數（board_lot）"
            if spec.action == "Sell":
                have = self._inventory_shares("US", spec.symbol, "cash")
                need = spec.qty + self._reserved_close("US", spec.symbol, "Sell", "cash")
                if have is not None and have < need:
                    return f"美股庫存 {have} 股，不足賣出 {spec.qty} 股（複委託不能放空）"
            p = float(spec.price)
            if p >= 1 and round(p, 2) != round(p, 6):
                return "美股 1 美元以上的價格最小單位為 0.01"
            if daytrade:
                if not mh.us_in_session(now, self.holidays) and (self.live or self.cfg["enforce_hours_in_simulation"]):
                    return "美股當沖只能在美東時間 09:30–16:00 盤中進場"
                if now >= mh.us_flatten_time(now) and mh.us_in_session(now, self.holidays):
                    return "已過美股當沖強制平倉時間（收盤前 15 分鐘）"
        if side and rules:
            ref = self._est_price(spec, c if spec.market == "TW" else None)
            errs = xr.check_levels(rules, side, ref or 0, ref)
            if errs:
                return "；".join(errs)
        if side:   # 只有開倉單受金額上限限制；平倉單不擋
            amt = self._est(spec, c if spec.market == "TW" else None)
            if amt <= 0:
                return "無法估算金額（沒有參考價），請改用限價"
            why = self.risk.check_new_order(spec.market, amt, side == "short", self.day(), self.short_exposure())
            if why:
                return why
        return None   # 停止新單時仍允許平倉單

    # ── 送單 ──
    def _send(self, o: dict) -> dict:
        spec = OrderSpec.from_dict(o["spec"])
        o["status"] = "sending"
        o["sent_at"] = self.now().isoformat(timespec="seconds")
        ok, msg = self.broker.place(o["id"], spec)
        o.update(self.broker.broker_ids(o["id"]) if hasattr(self.broker, "broker_ids") else {})
        if not ok:
            o["status"], o["msg"] = "rejected", msg
            self.log("reject", f"{o['id']} {spec.symbol} 送單失敗：{msg}", order=o["id"])
            return {"ok": False, "local_id": o["id"], "reason": msg}
        if o["status"] == "sending":
            o["status"] = "submitted"
        o["msg"] = msg or "已送出"
        if o["role"] == "entry":
            day = self.day()
            day["amount"] += self.risk.to_twd(o["market"], o["est"])
            day["orders"] += 1
        self.log("order", f"{o['id']} 送出 {spec.market} {spec.symbol} {spec.action} {CATEGORY_LABEL[spec.category]} "
                          f"{spec.qty} {spec.unit} {spec.price_type} {spec.price or ''} {spec.tif}",
                 order=o["id"], spec=o["spec"], role=o["role"])
        if o["role"] == "entry" and o.get("rules"):
            self.broker.subscribe(spec.market, spec.symbol, exit_unit(spec.unit) == "odd")
        return {"ok": True, "local_id": o["id"], "reason": o["msg"]}

    def cancel_order(self, oid: str, why: str = "使用者取消") -> tuple[bool, str]:
        with self.lock:
            o = self.d["orders"].get(oid)
            if not o or o["status"] not in ACTIVE:
                return False, "委託不存在或已結束"
            if o["status"] == "armed":
                o["status"], o["msg"] = "cancelled", why
                self.log("cancel", f"{oid} 取消等待中的委託：{why}", order=oid)
                self.store.save()
                return True, ""
            ok, msg = self.broker.cancel(oid)
            o["cancel_reason"] = why
            self.log("cancel", f"{oid} 送出刪單：{why}" + ("" if ok else f"（失敗：{msg}）"), order=oid)
            self.store.save()
            return ok, msg

    # ── 回報 ──
    def on_broker_event(self, ev: dict) -> None:
        with self.lock:
            lid = ev.get("local_id")
            o = self.d["orders"].get(lid) if lid else None
            if ev.get("kind") == "disconnected":
                self.log("error", "券商連線中斷")
                return
            if o is None:
                self.log("event", f"未對應的回報：{ev.get('task')} {ev.get('status')} {ev.get('symbol')} {ev.get('msg')}",
                         event=ev)
                return
            if ev.get("order_id"):
                o["order_id"] = ev["order_id"]
            k = ev["kind"]
            if k == "ack":
                if o["status"] in ("sending", "submitted"):
                    o["status"] = "submitted"
                o["msg"] = "委託成功"
            elif k == "reject":
                o["status"], o["msg"] = "rejected", ev.get("msg") or "委託失敗"
                if o["role"] == "entry":
                    self.day()["amount"] -= self.risk.to_twd(o["market"], o["est"])
                if o["role"] == "exit":
                    self._exit_failed(o)
            elif k == "deal":
                self._on_deal(o, int(ev.get("qty") or 0), float(ev.get("price") or 0), ev)
            elif k == "cancelled":
                o["status"] = "cancelled" if o["filled"] == 0 else "partial_cancelled"
                o["msg"] = o.get("cancel_reason") or "已取消"
                if o["role"] == "exit":
                    self._exit_failed(o, resend=True)
            elif k == "cancel_failed":
                o["msg"] = f"刪單失敗：{ev.get('msg')}"
            self.log("event", f"{lid} {k} {ev.get('qty') or ''} @ {ev.get('price') or ''} {ev.get('msg') or ''}",
                     order=lid, event=ev)
            self.store.save()

    def _on_deal(self, o: dict, qty: int, price: float, ev: dict) -> None:
        spec = o["spec"]
        if qty <= 0:
            return
        prev = o["filled"]
        o["filled"] = prev + qty
        o["avg"] = ((o["avg"] or 0) * prev + price * qty) / o["filled"]
        o["status"] = "filled" if o["filled"] >= spec["qty"] else "partial"
        sh = shares_of(spec["unit"], qty)
        fill = {"ts": self.now().isoformat(timespec="seconds"), "order": o["id"], "market": o["market"],
                "symbol": spec["symbol"], "action": spec["action"], "category": spec["category"],
                "unit": spec["unit"], "qty": qty, "shares": sh, "price": price, "role": o["role"]}
        self.d["fills"].append(fill)
        if o["role"] == "entry":
            pid = o.get("position")
            if pid is None:
                rules = xr.ExitRules.from_dict(o["rules"]) if o.get("rules") else None
                pid = self.store.next_id("P")
                o["position"] = pid
                kind = "cash" if o["market"] == "US" else spec["category"]
                self.d["positions"][pid] = self._new_pos(pid, o["market"], spec["symbol"], spec.get("name", ""),
                                                         o["side"], kind, exit_unit(spec["unit"]), spec["unit"],
                                                         sh, price, rules, source=o["id"],
                                                         currency=spec.get("currency", "MUT"))
                self.log("position", f"{pid} 建立部位 {spec['symbol']} {o['side']} {sh} 股 @ {price:g}", position=pid)
            else:
                p = self.d["positions"][pid]
                p["avg"] = (p["avg"] * p["shares"] + price * sh) / (p["shares"] + sh)
                p["shares"] += sh
                p["state"]["entry"] = p["avg"]
            self.d["positions"][pid]["opened_shares"] = self.d["positions"][pid].get("opened_shares", 0) + sh
        elif o["role"] == "exit":
            p = self.d["positions"].get(o.get("position"))
            if p:
                self._reduce(p, sh, price)
        else:   # 一般平倉單：若對應到程式監控中的部位，同步減少
            for p in self.d["positions"].values():
                if p["status"] in ("open", "exiting") and p["market"] == o["market"] and \
                        p["symbol"] == spec["symbol"] and close_method(p["kind"])[:2] == (spec["action"], spec["category"]):
                    self._reduce(p, min(sh, p["shares"]), price)
                    break

    def _reduce(self, p: dict, sh: int, price: float) -> None:
        sign = 1 if p["side"] == "long" else -1
        gross = (price - p["avg"]) * sh * sign
        cost = 0.0
        if p["market"] == "TW":
            dt = p["entry_date"] == mh.tw_trade_date(self.now()).isoformat()
            cost = twse.trade_cost(price * sh, "sell" if p["side"] == "long" else "buy", self.cfg["costs"], dt) + \
                twse.trade_cost(p["avg"] * sh, "buy" if p["side"] == "long" else "sell", self.cfg["costs"], dt)
        pnl = self.risk.to_twd(p["market"], gross) - cost
        p["shares"] -= sh
        p["realized"] = p.get("realized", 0.0) + pnl
        self.day()["realized"] += pnl
        if p["shares"] <= 0:
            p["shares"], p["status"] = 0, "closed"
            p["closed_at"] = self.now().isoformat(timespec="seconds")
            self.log("position", f"{p['id']} 平倉完成 {p['symbol']}，損益約 {p['realized']:,.0f} 元", position=p["id"])

    def _new_pos(self, pid, market, symbol, name, side, kind, unit, entry_unit, shares, avg, rules, source,
                 currency="MUT") -> dict:
        today = mh.trade_date(market, self.now()).isoformat()
        return {"id": pid, "market": market, "symbol": symbol, "name": name, "side": side, "kind": kind,
                "unit": unit, "entry_unit": entry_unit, "shares": shares, "avg": avg, "entry_date": today,
                "rules": rules.to_dict() if rules else None, "status": "open", "source": source,
                "state": {"entry": avg, "best": avg}, "realized": 0.0, "exit_orders": [], "exit_reason": "",
                "retries": 0, "currency": currency, "day_ref": {}}

    # ── 行情與時間觸發 ──
    def on_tick(self, market: str, symbol: str, odd: bool, price: float, ts: str = "") -> None:
        with self.lock:
            self.last[self.qkey(market, symbol, odd)] = price
            changed = False
            for o in list(self.d["orders"].values()):
                if o["status"] == "armed" and o["market"] == market and o["spec"]["symbol"] == symbol and \
                        (o["spec"]["unit"] == "odd") == odd:
                    changed |= self._try_arm(o, price)
            for p in list(self.d["positions"].values()):
                if p["status"] != "open" or p["market"] != market or p["symbol"] != symbol or (p["unit"] == "odd") != odd:
                    continue
                p["last"] = price
                p["day_ref"].setdefault(self.risk_day(), price)
                if not p.get("rules"):
                    continue
                rules = xr.ExitRules.from_dict(p["rules"])
                st = xr.MonitorState(p["side"], p["state"]["entry"], p["state"]["best"], p["entry_date"])
                why = xr.price_trigger(rules, st, price)
                if st.best != p["state"]["best"]:
                    p["state"]["best"] = st.best
                    changed = True
                if why:
                    self._exit(p, why)
                    changed = True
            self._check_daily_loss()
            if changed:
                self.store.save()

    def _try_arm(self, o: dict, price: float) -> bool:
        rules = xr.ExitRules.from_dict(o["rules"])
        state = xr.entry_window_state(rules, o["market"], self.now())
        if state != "open" or not xr.breakout_hit(rules, o["side"], price):
            return False
        spec = OrderSpec.from_dict(o["spec"])
        why = self._precheck(spec, rules, o["side"], for_send=True)
        if why:
            o["status"], o["msg"] = "rejected", f"進場條件成立但檢查未通過：{why}"
            self.log("reject", f"{o['id']} {o['msg']}", order=o["id"])
            return True
        self.log("trigger", f"{o['id']} 進場條件成立（{price:g}），送出委託", order=o["id"])
        self._send(o)
        return True

    def on_timer(self) -> None:
        with self.lock:
            now = self.now()
            changed = False
            for o in list(self.d["orders"].values()):
                if o["status"] not in ACTIVE or not o.get("rules") or o["role"] != "entry":
                    continue
                rules = xr.ExitRules.from_dict(o["rules"])
                if rules.entry_start and xr.entry_window_state(rules, o["market"], now) == "closed":
                    if o["status"] == "armed":
                        o["status"], o["msg"] = "expired", "進場時間窗已過，未觸發"
                        self.log("expire", f"{o['id']} {o['msg']}", order=o["id"])
                        changed = True
                    elif o["filled"] < o["spec"]["qty"] and not o.get("cancel_reason"):
                        self.cancel_order(o["id"], "進場時間窗結束，刪除未成交部分")
                        changed = True
                if rules.position_type == "daytrade" and o["filled"] < o["spec"]["qty"] and not o.get("cancel_reason"):
                    dl = xr.flatten_deadline(rules, o["market"], date.fromisoformat(o["trade_date"]), now, self.holidays)
                    if dl and now >= dl:
                        if o["status"] == "armed":
                            o["status"], o["msg"] = "expired", "已到當沖平倉時間，未進場"
                        else:
                            self.cancel_order(o["id"], "已到當沖平倉時間，刪除未成交進場單")
                        changed = True
            for p in list(self.d["positions"].values()):
                if p["status"] == "open" and p.get("rules"):
                    rules = xr.ExitRules.from_dict(p["rules"])
                    why = xr.time_trigger(rules, p["market"], date.fromisoformat(p["entry_date"]), now, self.holidays)
                    if why:
                        self._exit(p, why)
                        changed = True
                elif p["status"] == "exiting":
                    changed |= self._exit_watchdog(p)
                elif p["status"] == "exit_waiting":
                    p["status"] = "open"
                    self._exit(p, p.get("exit_reason") or "等待中的出場")
                    changed = True
            changed |= self._check_daily_loss()
            if changed:
                self.store.save()

    # ── 出場 ──
    def _exit_spec(self, p: dict) -> list[OrderSpec]:
        act, cat, _ = close_method(p["kind"])
        last = p.get("last") or self.last.get(self.qkey(p["market"], p["symbol"], p["unit"] == "odd")) or p["avg"]
        ex = self.cfg["exits"]
        specs = []
        if p["market"] == "US":
            slip = ex["us_slip_pct"] / 100 * (1 + p.get("retries", 0))
            px = last * (1 - slip) if act == "Sell" else last * (1 + slip)
            px = round(px, 2) if px >= 1 else round(px, 4)
            specs.append(OrderSpec("US", p["symbol"], act, "cash", "shares", "LMT", "ROD", p["shares"], px,
                                   p.get("currency", "MUT")))
            return specs
        c = self.broker.tw_contract(p["symbol"]) or {}
        lots, odd = (p["shares"] // 1000, p["shares"] % 1000) if p["unit"] == "common" else (0, p["shares"])
        if lots:
            if ex["tw_common_price"] == "market":
                specs.append(OrderSpec("TW", p["symbol"], act, cat, "common", "MKT", "IOC", lots))
            else:
                specs.append(OrderSpec("TW", p["symbol"], act, cat, "common",
                                       "LIMIT_DOWN" if act == "Sell" else "LIMIT_UP", "ROD", lots))
        if odd:
            n = ex["tw_odd_slip_ticks"] * (1 + p.get("retries", 0))
            px = twse.ticks_away(last, -n if act == "Sell" else n)
            if c.get("bear") and c.get("bull"):
                px = min(max(px, c["bear"]), c["bull"])
            specs.append(OrderSpec("TW", p["symbol"], act, cat, "odd", "LMT", "ROD", odd, px))
        return specs

    def _exit(self, p: dict, why: str) -> None:
        if p["status"] not in ("open",):
            return
        p["exit_reason"] = why
        # 先刪掉這個部位尚未成交的進場單
        src = self.d["orders"].get(p.get("source"))
        if src and src["status"] in ACTIVE:
            self.cancel_order(src["id"], "出場前刪除未成交的進場單")
        specs = self._exit_spec(p)
        if self._check_hours():
            for s in specs:
                ok, w = self._can_order_now(s)
                if not ok:
                    p["status"] = "exit_waiting"
                    if p.get("wait_logged") == why:
                        return
                    p["wait_logged"] = why
                    self.log("exit_wait", f"{p['id']} 出場條件成立（{why}），但{w}，開盤後送出", position=p["id"])
                    return
        p["status"] = "exiting"
        method = close_method(p["kind"])[2]
        self.log("exit", f"{p['id']} {p['symbol']} 觸發出場：{why}，以{method}平倉", position=p["id"])
        for s in specs:
            oid = self.store.next_id("X")
            o = {"id": oid, "market": p["market"], "spec": s.to_dict(), "role": "exit", "side": None, "rules": None,
                 "status": "sending", "filled": 0, "avg": None, "created": self.now().isoformat(timespec="seconds"),
                 "msg": why, "est": 0.0, "trade_date": p["entry_date"], "position": p["id"]}
            self.d["orders"][oid] = o
            p["exit_orders"].append(oid)
            self._send(o)
            if o["status"] == "rejected":
                self._exit_failed(o)

    def _exit_failed(self, o: dict, resend: bool = False) -> None:
        p = self.d["positions"].get(o.get("position"))
        if not p or p["status"] == "closed":
            return
        if any(self.d["orders"][x]["status"] in ACTIVE for x in p["exit_orders"] if x in self.d["orders"]):
            return
        p["retries"] = p.get("retries", 0) + 1
        if p["retries"] > self.cfg["exits"]["max_retries"]:
            p["status"] = "exit_failed"
            self.log("error", f"{p['id']} {p['symbol']} 平倉失敗 {p['retries'] - 1} 次，請立即手動處理", position=p["id"])
            return
        p["status"] = "open"
        self.log("exit_retry", f"{p['id']} 平倉單{'未成交已刪單' if resend else '被拒'}，第 {p['retries']} 次重送", position=p["id"])
        self._exit(p, p["exit_reason"])

    def _exit_watchdog(self, p: dict) -> bool:
        """出場單（限價）逾時未成交就刪單，刪單回報後由 _exit_failed 以更不利的價格重送。"""
        now = self.now()
        changed = False
        for oid in p["exit_orders"]:
            o = self.d["orders"].get(oid)
            if not o or o["status"] not in ("submitted", "partial") or o.get("cancel_reason"):
                continue
            if o["spec"]["price_type"] != "LMT":
                continue
            sent = datetime.fromisoformat(o["sent_at"])
            if (now - sent).total_seconds() >= self.cfg["exits"]["retry_sec"]:
                self.cancel_order(oid, "出場限價單逾時未成交，刪單後重掛")
                changed = True
        return changed

    # ── 風控：單日最大虧損 ──
    def pnl_today(self) -> float:
        tot = self.day()["realized"]
        rd = self.risk_day()
        for p in self.d["positions"].values():
            if p["status"] not in ("open", "exiting", "exit_waiting", "exit_failed") or not p.get("last"):
                continue
            if p["source"] != "inventory" and p["entry_date"] == rd:
                ref = p["avg"]                       # 今天建立的部位：從成本算起
            else:
                ref = p["day_ref"].get(rd, p["avg"])  # 舊部位：從今天第一筆報價算起
            sign = 1 if p["side"] == "long" else -1
            tot += self.risk.to_twd(p["market"], (p["last"] - ref) * p["shares"] * sign)
        return tot

    def _check_daily_loss(self) -> bool:
        day = self.day()
        if day["halted"]:
            return False
        pnl = self.pnl_today()
        if not self.risk.loss_breached(pnl):
            return False
        day["halted"] = True
        day["halt_reason"] = f"單日虧損 {pnl:,.0f} 元已達上限 {self.risk.limits.max_daily_loss:,.0f} 元"
        self.log("halt", day["halt_reason"] + "：停止新委託並平掉當沖部位")
        for o in list(self.d["orders"].values()):
            if o["role"] == "entry" and o["status"] in ACTIVE:
                self.cancel_order(o["id"], "觸發單日最大虧損")
        for p in list(self.d["positions"].values()):
            if p["status"] == "open" and (p.get("rules") or {}).get("position_type") == "daytrade":
                self._exit(p, "觸發單日最大虧損，平掉當沖部位")
        return True

    # ── 緊急按鈕 ──
    def cancel_all(self) -> dict:
        with self.lock:
            n, fails = 0, []
            for o in list(self.d["orders"].values()):
                if o["status"] in ACTIVE and o["role"] != "exit":
                    ok, msg = self.cancel_order(o["id"], "緊急：全部取消")
                    n += ok
                    if not ok:
                        fails.append(f"{o['id']}：{msg}")
            self.log("emergency", f"全部取消：{n} 筆" + (f"，失敗 {len(fails)} 筆" if fails else ""))
            self.store.save()
            return {"ok": not fails, "cancelled": n, "failed": fails}

    def flatten_all(self) -> dict:
        with self.lock:
            self.cancel_all()
            n = 0
            for p in list(self.d["positions"].values()):
                if p["status"] in ("open", "exit_failed", "exit_waiting"):
                    p["status"], p["retries"] = "open", 0
                    self._exit(p, "緊急：全部平倉")
                    n += 1
            self.day()["halted"] = True
            self.day()["halt_reason"] = self.day()["halt_reason"] or "使用者按下全部平倉"
            self.log("emergency", f"全部平倉：{n} 個部位，並停止今日新委託")
            self.store.save()
            return {"ok": True, "positions": n}

    def resume(self) -> dict:
        with self.lock:
            day = self.day()
            if self.risk.loss_breached(self.pnl_today()):
                return {"ok": False, "error": "今日虧損仍超過上限，無法恢復"}
            day["halted"], day["halt_reason"] = False, ""
            self.log("resume", "使用者恢復今日新委託")
            self.store.save()
            return {"ok": True}

    def stop_monitor(self, pid: str) -> dict:
        with self.lock:
            p = self.d["positions"].get(pid)
            if not p or p["status"] not in ("open", "exit_waiting", "exit_failed"):
                return {"ok": False, "error": "部位不存在或正在平倉"}
            p["status"] = "detached"
            self.log("detach", f"{pid} 停止監控（庫存不變）", position=pid)
            self.store.save()
            return {"ok": True}

    # ── 重啟接續 ──
    def resume_after_restart(self) -> list[str]:
        notes = []
        with self.lock:
            for o in self.d["orders"].values():
                if o["status"] in ("sending", "submitted", "partial"):
                    if str(o.get("order_id") or "").startswith("SIM"):
                        o["status"], o["msg"] = "cancelled", "本機模擬委託，下單程式重啟後失效"
                        notes.append(f"{o['id']} {o['spec']['symbol']}：{o['msg']}")
                        continue
                    info = self.broker.reattach(o["id"], o["market"], o.get("order_id") or o.get("org_seqnum"))
                    if info is None:
                        o["status"], o["msg"] = "unknown", "重啟後找不到這筆委託，請到券商軟體對帳"
                        notes.append(f"{o['id']} {o['spec']['symbol']}：{o['msg']}")
                    elif info["filled"] > o["filled"]:
                        self._on_deal(o, info["filled"] - o["filled"], info["avg"] or 0, {})
                        notes.append(f"{o['id']} 補上重啟期間的成交 {info['filled']}")
                elif o["status"] == "armed":
                    s = o["spec"]
                    self.broker.subscribe(o["market"], s["symbol"], s["unit"] == "odd")
            for p in self.d["positions"].values():
                if p["status"] in ("open", "exiting", "exit_waiting"):
                    self.broker.subscribe(p["market"], p["symbol"], p["unit"] == "odd")
                    if p["status"] == "exiting" and not any(
                            self.d["orders"].get(x, {}).get("status") in ACTIVE for x in p["exit_orders"]):
                        p["status"] = "open"
                        notes.append(f"{p['id']} 重啟前正在平倉，重新評估出場條件")
            self.store.save()
        for n in notes:
            self.log("restart", n)
        return notes

    # ── 狀態 ──
    def snapshot(self) -> dict:
        with self.lock:
            day = self.day()
            orders = sorted(self.d["orders"].values(), key=lambda o: o["created"], reverse=True)[:300]
            pos = [p for p in self.d["positions"].values() if p["status"] != "closed"] + \
                sorted([p for p in self.d["positions"].values() if p["status"] == "closed"],
                       key=lambda p: p.get("closed_at", ""), reverse=True)[:50]
            return {
                "mode": self.mode, "now": self.now().isoformat(timespec="seconds"),
                "risk_day": self.risk_day(), "day": day, "pnl_today": round(self.pnl_today(), 0),
                "limits": self.risk.limits.__dict__, "short_exposure": round(self.short_exposure(), 0),
                "orders": orders, "positions": pos, "fills": self.d["fills"][-200:][::-1],
                "accounts": {"tw": bool(self.broker.has_tw), "us": bool(self.broker.has_us),
                             "us_local_sim": bool(getattr(self.broker, "us_is_local_sim", False)),
                             "tw_local_sim": bool(getattr(self.broker, "tw_is_local_sim", False))},
                "us_dst": mh.utc_offset_hours("US", self.now()) == -4,
                "us_flatten_tpe": mh.us_flatten_time(self.now()).astimezone(mh.TPE).strftime("%H:%M"),
                "log": self.store.tail_log(150)[::-1],
            }

    def inventory(self) -> dict:
        with self.lock:
            out, errs = [], []
            for m, fn in (("TW", "tw_inventory"), ("US", "us_inventory")):
                if not (self.broker.has_tw if m == "TW" else self.broker.has_us):
                    continue
                try:
                    out += getattr(self.broker, fn)()
                except Exception as e:  # noqa: BLE001
                    errs.append(f"{m} 庫存查詢失敗：{e}")
            for r in out:
                kind = {"odd": "cash", "lend": "lend_sell"}.get(r["kind"], r["kind"])
                r["monitored_by"] = next((p["id"] for p in self.d["positions"].values()
                                          if p["status"] in ("open", "exiting") and p["market"] == r["market"]
                                          and p["symbol"] == r["symbol"] and p["kind"] == kind), None)
            return {"ok": not errs, "items": out, "errors": errs}


def run_timer(engine: Engine, stop: threading.Event, every: float = 1.0) -> None:
    while not stop.is_set():
        try:
            engine.on_timer()
        except Exception as e:  # noqa: BLE001
            engine.log("error", f"計時器錯誤：{e}")
        _time.sleep(every)


def _deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _deep_merge(a[k], v) if isinstance(v, dict) and isinstance(a.get(k), dict) else v
    return out
