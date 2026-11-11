"""凱基 SuperPy（kgisuperpy 2.1.2）介接層。

engine 只透過這裡呼叫券商，方便用 mock 物件測試。對應的 SDK 介面（皆以實際原始碼確認）：

台股（api.set_Account 之後）
- 下單：api.Order.create_order(action, symbol, qty, price, time_in_force, order_cond, odd_lot, name) → Trade
- 刪單：api.Order.cancel_order(order_id)、api.Order.cancel_order_all()
- 回報：api.Order.set_event(cb)；Event 有 task、status、order_id、seqno、quantity、price、msg
- 商品：api.Order.contract('dic') → {symbol: Contract}；Contract.day_trade 為 'Yes'／'OnlyBuy'／'No'／0
- 信用額度：api.Order.TSECreditInfo(symbol) → DataFrame（MarignQty、ShortQty、ErrCode、ErrMsg…）
- 庫存：api.Account.InventorySum('B')（股數）→ NETQTY0 現股、NETQTY3 融資、NETQTY4 融券、NETQTY9 零股、NETQTY5 借券
- 行情：api.Quote.set_cb_tick(cb)、api.Quote.subscribe_tick(symbol, odd_lot=bool)；tick 有 symbol、close、odd_lot

複委託（api.set_SubAccount 之後）
- 下單：api.SubOrder.create_order(action, symbol, qty, price, currency, name) → Trade（只有限價 ROD）
- 刪單：api.SubOrder.cancel_order(org_seq)
- 商品：api.SubOrder.contract('dic')；只接受 market == 'US'，qty 需為 board_lot 倍數
- 庫存：api.SubAccount.StockPositionReport() → symbol、Qty、market_price
- 行情：api.USQuote.set_cb_tick(cb)、api.USQuote.subscribe_tick(symbol)

凱基手冊明載複委託沒有模擬環境，所以模擬模式下美股改用本機模擬成交（LocalSim），不送到券商。
演練模式（dry_run=True）用正式環境登入，讀真實行情、庫存與商品資料，但台股與美股委託都只在本機模擬成交。
"""

from __future__ import annotations

import itertools
import threading
import time
from datetime import datetime
from typing import Any, Callable

from .kgi_spec import OrderSpec

US_FAIL_STATUS = {"委託失敗", "處理失敗", "無效單", "作癈單", "作廢單", "逾期單"}
US_OK_STATUS = {"委託成功", "已下單至交易室"}


def _name(x) -> str:
    """Enum 取 name；字串原樣回傳。"""
    return getattr(x, "name", None) or (str(x) if x is not None else "")


def _num(v, default=0.0):
    """數字欄位可能是字串、帶千分位逗號或空白（例如 " 1,000 "）。"""
    if isinstance(v, str):
        v = v.replace(",", "").strip()
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return default if f != f else f       # NaN 視為沒有值


def _rows(df) -> list[dict]:
    if df is None:
        return []
    if hasattr(df, "to_dict"):
        try:
            return df.to_dict("records")
        except TypeError:
            pass
    return list(df)


class BrokerError(RuntimeError):
    pass


class KgiBroker:
    """真實 SuperPy（模擬或正式環境）。"""

    def __init__(self, kgi_module: Any = None, simulation: bool = True, log: Callable[..., Any] | None = None,
                 dry_run: bool = False):
        if kgi_module is None:
            import kgisuperpy as kgi_module  # noqa: N813  Windows 上安裝 kgisuperpy 後才有
        self.kgi = kgi_module
        self.simulation = simulation
        self.dry_run = dry_run and not simulation
        self.api = None
        self.tw_account = None
        self.us_account = None
        self._log = log or (lambda *a, **k: None)
        self._on_event: Callable[[dict], None] = lambda e: None
        self._on_tick: Callable[[str, str, bool, float, str], None] = lambda *a: None
        self._trades: dict[str, tuple[str, Any]] = {}      # local_id → (market, Trade)
        self._lock = threading.RLock()
        self._subs: set[tuple[str, str, bool]] = set()
        self._tw_contracts = None
        self._us_contracts = None
        self.sim: LocalSim | None = None
        # 凱基測試說明要求模擬環境的下單間隔大於 1 秒；正式環境預設不限制
        self.min_interval = 1.1 if simulation else 0.0
        self._last_place = 0.0
        self.raw_info: dict[str, dict] = {}
        self._reconnecting = False
        self._closing = False     # 最近一次庫存查詢：券商回傳幾列、有哪些欄位（只供診斷）

    # ── 連線與帳號 ──
    def login(self, person_id: str, password: str, tw_account: str | None, us_account: str | None) -> list[dict]:
        if not person_id or not password:
            raise BrokerError("缺少身分證字號或密碼（請填 trader/.env 的 KGI_PERSON_ID、KGI_PERSON_PWD）")
        self.api = self.kgi.login(person_id, password, simulation=self.simulation)
        if not hasattr(self.api, "show_account"):
            raise BrokerError("登入失敗（請看上方 SuperPy 訊息：帳密、CA 憑證或連線）")
        accounts = list(self.api.show_account() or [])

        def pick(acc: str | None, flag: str) -> str | None:
            if not acc:
                return None
            for row in accounts:
                if str(row.get("account")) == acc and flag in str(row.get("account_flag", "")):
                    return acc
            raise BrokerError(f"帳號 {acc} 不在登入帳號清單中，或不是{flag}帳號；可用帳號：" +
                              "、".join(f"{r.get('account')}（{r.get('account_flag')}）" for r in accounts))

        self.tw_account = pick(tw_account, "證券")
        self.us_account = pick(us_account, "複委託")
        if not self.tw_account and not self.us_account:
            raise BrokerError("請在 trader/.env 指定 KGI_TW_ACCOUNT（證券）或 KGI_US_ACCOUNT（複委託），"
                              "程式不會自動選第一個帳號。可用帳號：" +
                              "、".join(f"{r.get('account')}（{r.get('account_flag')}）" for r in accounts))
        if self.tw_account:
            self.api.set_Account(self.tw_account)
            self.api.Order.set_event(lambda ev: self._sdk_event("TW", ev))
        if self.us_account and not self.simulation:   # 演練模式也設定，用來讀商品檔與庫存
            self.api.set_SubAccount(self.us_account)
            self.api.SubOrder.set_event(lambda ev: self._sdk_event("US", ev))
        if self.simulation or self.dry_run:
            self.sim = LocalSim(lambda e: self._on_event(e))
        if hasattr(self.api, "Quote"):
            self.api.Quote.set_cb_tick(self._tw_tick)
        if hasattr(self.api, "USQuote"):
            self.api.USQuote.set_cb_tick(self._us_tick)
        if hasattr(self.api, "set_disconnect_cb"):
            self.api.set_disconnect_cb(self._disconnected)
        return accounts

    def logout(self):
        self._closing = True
        if self.api is not None and hasattr(self.api, "logout"):
            self.api.logout()

    @property
    def has_tw(self) -> bool:
        return bool(self.tw_account)

    @property
    def has_us(self) -> bool:
        return bool(self.us_account) or self.simulation

    @property
    def us_is_local_sim(self) -> bool:
        return self.simulation or self.dry_run

    @property
    def tw_is_local_sim(self) -> bool:
        return self.dry_run

    def _local(self, market: str) -> bool:
        return self.dry_run or (market == "US" and self.simulation)

    def set_handlers(self, on_event, on_tick):
        self._on_event, self._on_tick = on_event, on_tick

    # ── 斷線自動重連 ──
    RECONNECT_WAITS = (5, 15, 30, 60, 120, 300)     # 秒；之後每 300 秒再試，直到成功或程式結束

    def _disconnected(self):
        # SDK 的斷線回呼在 SDK 執行緒中，不可在這裡阻塞：另開執行緒重連
        self._log("error", "SuperPy 斷線，開始自動重新登入")
        self._on_event({"kind": "disconnected", "local_id": None, "msg": "斷線"})
        if self._reconnecting or self._closing:
            return
        self._reconnecting = True
        threading.Thread(target=self._reconnect_loop, daemon=True).start()

    def _reconnect_loop(self):
        n = 0
        try:
            while not self._closing:
                wait = self.RECONNECT_WAITS[min(n, len(self.RECONNECT_WAITS) - 1)]
                time.sleep(wait)
                n += 1
                try:
                    if self.relogin():
                        self._log("reconnect", f"第 {n} 次重新登入成功，已恢復帳號、回報與 {len(self._subs)} 檔行情訂閱")
                        self._on_event({"kind": "reconnected", "local_id": None, "msg": "重新登入成功"})
                        return
                    self._log("error", f"第 {n} 次重新登入失敗，{self.RECONNECT_WAITS[min(n, len(self.RECONNECT_WAITS) - 1)]} 秒後再試")
                except Exception as e:  # noqa: BLE001  重連失敗不可讓執行緒結束
                    self._log("error", f"第 {n} 次重新登入發生錯誤：{e}")
        finally:
            self._reconnecting = False

    def relogin(self) -> bool:
        """依官方重登範例呼叫既有物件的 api.login()，再恢復帳號、回報 callback 與行情訂閱。"""
        self.api.login()
        accounts = {str(r.get("account")) for r in (self.api.show_account() or [])}
        need = {a for a in (self.tw_account, self.us_account) if a}
        if not need or not need <= accounts:
            return False
        if self.tw_account:
            self.api.set_Account(self.tw_account)
            self.api.Order.set_event(lambda ev: self._sdk_event("TW", ev))
        if self.us_account and not self.simulation:
            self.api.set_SubAccount(self.us_account)
            self.api.SubOrder.set_event(lambda ev: self._sdk_event("US", ev))
        if hasattr(self.api, "Quote"):
            self.api.Quote.set_cb_tick(self._tw_tick)
        if hasattr(self.api, "USQuote"):
            self.api.USQuote.set_cb_tick(self._us_tick)
        if hasattr(self.api, "set_disconnect_cb"):
            self.api.set_disconnect_cb(self._disconnected)
        subs, self._subs = set(self._subs), set()
        for m, sym, odd in subs:
            try:
                self.subscribe(m, sym, odd)
            except Exception as e:  # noqa: BLE001
                self._log("error", f"重新訂閱 {m} {sym} 失敗：{e}")
        return True

    # ── 商品、信用、庫存 ──
    def tw_contract(self, symbol: str) -> dict | None:
        if self._tw_contracts is None:
            self._tw_contracts = self.api.Order.contract("dic") or {}
        c = self._tw_contracts.get(symbol)
        if c is None:
            return None
        g = (lambda k: c.get(k)) if isinstance(c, dict) else (lambda k: getattr(c, k, None))
        return {"symbol": symbol, "name": g("name"), "day_trade": g("day_trade"),
                "bull": _num(g("bull_limit"), None), "bear": _num(g("bear_limit"), None),
                "ref": _num(g("ref_price"), None)}

    def tw_credit(self, symbol: str) -> dict:
        df = self.api.Order.TSECreditInfo(symbol)
        rows = _rows(df)
        if not rows:
            return {"ok": False, "msg": "查不到信用額度（TSECreditInfo 無回應）"}
        r = rows[0]
        err = str(r.get("ErrCode", "0")).strip()
        return {"ok": err in ("", "0", "0000"), "msg": str(r.get("ErrMsg", "") or ""),
                "margin_qty": _num(r.get("MarignQty"), None),     # SDK 欄位拼字即為 MarignQty
                "short_qty": _num(r.get("ShortQty"), None),
                "margin_status": r.get("MarginStatus"), "short_status": r.get("ShortStatus"),
                "raw": {k: str(v) for k, v in r.items()}}

    def tw_inventory(self) -> list[dict]:
        out = []
        rows = _rows(self.api.Account.InventorySum("B"))
        self.raw_info["TW"] = {"rows": len(rows), "columns": list(rows[0].keys())[:60] if rows else []}
        for r in rows:
            sym = str(r.get("Symbol", "")).strip()
            last = _num(r.get("RLPRICE"), None)
            for kind, qcol, pcol in (("cash", "NETQTY0", "AVG_PRICE0"), ("odd", "NETQTY9", "AVG_PRICE0"),
                                     ("margin", "NETQTY3", "AVG_PRICE3"), ("short", "NETQTY4", "AVG_PRICE4"),
                                     ("lend", "NETQTY5", None)):
                q = int(_num(r.get(qcol), 0))
                if q:
                    out.append({"market": "TW", "symbol": sym, "name": r.get("SymbolName", ""), "kind": kind,
                                "shares": q, "avg": _num(r.get(pcol), None) if pcol else None, "last": last})
        return out

    def us_contract(self, symbol: str) -> dict | None:
        if self.simulation:
            return {"key": symbol, "board_lot": 1, "market": "US"}     # 本機模擬，不查券商商品檔
        if self._us_contracts is None:
            self._us_contracts = self.api.SubOrder.contract("dic") or {}
        d = self._us_contracts
        key = symbol if symbol in d else next((k for k in d if str(k).split(".")[0] == symbol), None)
        if key is None:
            return None
        c = d[key]
        g = (lambda k: c.get(k)) if isinstance(c, dict) else (lambda k: getattr(c, k, None))
        return {"key": key, "board_lot": int(_num(g("board_lot"), 1)) or 1, "market": str(g("market"))}

    def us_inventory(self) -> list[dict]:
        if self.simulation:
            return self.sim.inventory() if self.sim else []
        out = []
        rows = _rows(self.api.SubAccount.StockPositionReport())
        self.raw_info["US"] = {"rows": len(rows), "columns": list(rows[0].keys())[:60] if rows else []}
        for r in rows:
            q = int(_num(r.get("Qty"), 0))
            if q:
                out.append({"market": "US", "symbol": str(r.get("symbol", "")).split(".")[0],
                            "name": r.get("symbol_name", ""), "kind": "cash", "shares": q,
                            "avg": None, "last": _num(r.get("market_price"), None)})
        return out

    # ── 下單 ──
    def _tw_args(self, spec: OrderSpec) -> dict:
        k = self.kgi
        price = {
            "LMT": spec.price, "MKT": k.PriceType.MKT, "LIMIT_UP": k.PriceType.LimitUp,
            "LIMIT_DOWN": k.PriceType.LimitDown, "REFERENCE": k.PriceType.Reference,
        }[spec.price_type]
        cond = {"cash": k.OrderCond.CASH, "margin": k.OrderCond.MARGIN, "short": k.OrderCond.SHORT_SELLING,
                "lend_sell": k.OrderCond.Lend_SELLING, "daytrade_short": k.OrderCond.CASH_SELLING}[spec.category]
        odd = {"common": k.OddLot.Common, "odd": k.OddLot.Odd, "odd_after": k.OddLot.Odd_AfterMarket,
               "fixing": k.OddLot.Fixing}[spec.unit]
        return dict(action=getattr(k.Action, spec.action), symbol=spec.symbol, qty=int(spec.qty), price=price,
                    time_in_force=getattr(k.TimeInForce, spec.tif), order_cond=cond, odd_lot=odd)

    def _throttle(self) -> None:
        wait = self._last_place + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_place = time.monotonic()

    def place(self, local_id: str, spec: OrderSpec) -> tuple[bool, str]:
        if self._local(spec.market):
            if spec.market == "TW" and not self.has_tw:
                return False, "未設定證券帳號（KGI_TW_ACCOUNT）"
            self.sim.place(local_id, spec)
            return True, ("演練：本機模擬委託，未送到凱基" if self.dry_run else "美股本機模擬委託（凱基無複委託模擬環境）")
        self._throttle()
        try:
            if spec.market == "TW":
                if not self.has_tw:
                    return False, "未設定證券帳號（KGI_TW_ACCOUNT）"
                trade = self.api.Order.create_order(**self._tw_args(spec), name=local_id[-10:])
            else:
                if not self.us_account:
                    return False, "未設定複委託帳號（KGI_US_ACCOUNT）"
                c = self.us_contract(spec.symbol)
                trade = self.api.SubOrder.create_order(
                    action=getattr(self.kgi.Action, spec.action), symbol=c["key"], qty=int(spec.qty),
                    price=float(spec.price), currency=getattr(self.kgi.Currency, spec.currency), name=local_id[-10:])
        except (ValueError, TypeError, KeyError) as e:
            return False, f"SuperPy 拒絕：{e}"
        if trade is None:
            return False, "SuperPy 未回傳委託（請看終端機的 SecurityOrder 錯誤訊息）"
        with self._lock:
            self._trades[local_id] = (spec.market, trade)
        return True, ""

    def cancel(self, local_id: str) -> tuple[bool, str]:
        with self._lock:
            rec = self._trades.get(local_id)
        if self.sim and self.sim.has(local_id):
            return self.sim.cancel(local_id)
        if rec is None:
            return False, "找不到這筆委託（可能是重啟前送出的委託，請到券商軟體刪單）"
        market, trade = rec
        try:
            if market == "TW":
                oid = getattr(trade.order, "order_id", None)
                if not oid:
                    return False, "委託尚未取得委託書號，稍後再刪"
                self.api.Order.cancel_order(oid)
            else:
                seq = getattr(trade.order, "org_seqnum", None)
                if not seq:
                    return False, "委託尚未取得原始序號，稍後再刪"
                self.api.SubOrder.cancel_order(seq)
        except (ValueError, TypeError, KeyError) as e:
            return False, f"刪單失敗：{e}"
        return True, ""

    def broker_ids(self, local_id: str) -> dict:
        rec = self._trades.get(local_id)
        if not rec:
            return {}
        o = rec[1].order
        return {"order_id": getattr(o, "order_id", None), "org_seqnum": getattr(o, "org_seqnum", None)}

    def reattach(self, local_id: str, market: str, order_id: str | None) -> dict | None:
        """重啟後用委託書號找回 Trade，回傳 {status, filled}；找不到回傳 None。"""
        if not order_id:
            return None
        try:
            src = self.api.Order.get_trades(True) if market == "TW" else self.api.SubOrder.get_trades(True)
        except Exception:  # noqa: BLE001  查詢失敗不影響啟動
            return None
        for key, trade in (src or {}).items():
            o = trade.order
            if order_id in (key, getattr(o, "order_id", None), getattr(o, "org_seqnum", None)):
                with self._lock:
                    self._trades[local_id] = (market, trade)
                deals = getattr(trade.order_status, "deals", []) or []
                return {"status": _name(trade.order_status.status),
                        "filled": sum(int(_num(d.quantity, 0)) for d in deals),
                        "avg": (sum(_num(d.price) * _num(d.quantity) for d in deals) /
                                max(1, sum(_num(d.quantity) for d in deals))) if deals else None}
        return None

    # ── 行情 ──
    def subscribe(self, market: str, symbol: str, odd: bool = False) -> None:
        key = (market, symbol, odd)
        if key in self._subs:
            return
        if market == "TW":
            self.api.Quote.subscribe_tick(symbol, odd_lot=odd)
        elif hasattr(self.api, "USQuote"):
            self.api.USQuote.subscribe_tick(symbol)
        self._subs.add(key)

    def _tw_tick(self, *args):   # 不可加型別標註：SuperPy 會檢查 __annotations__，字串 'None' 會被拒絕
        t = args[-1]
        price = _num(getattr(t, "close", None), None)
        if price:
            sym, odd = str(t.symbol), bool(getattr(t, "odd_lot", False))
            if self.dry_run and self.sim:
                self.sim.on_tick("TW", sym, odd, price)
            self._on_tick("TW", sym, odd, price, str(getattr(t, "datetime", "")))

    def _us_tick(self, *args):   # 不可加型別標註：SuperPy 會檢查 __annotations__，字串 'None' 會被拒絕
        t = args[-1]
        price = _num(getattr(t, "close", None), None)
        if price:
            sym = str(t.symbol).split(".")[0]
            if self.sim:
                self.sim.on_tick("US", sym, False, price)
            self._on_tick("US", sym, False, price, str(getattr(t, "datetime", "")))

    # ── 回報 ──
    def _find_local(self, market: str, ev) -> str | None:
        oid = getattr(ev, "order_id", None)
        seq = getattr(ev, "org_seqnum", None)
        cnt = getattr(ev, "seqno", None)
        with self._lock:
            for lid, (m, trade) in self._trades.items():
                if m != market:
                    continue
                o = trade.order
                if oid and oid == getattr(o, "order_id", None):
                    return lid
                if seq and seq == getattr(o, "org_seqnum", None):
                    return lid
                if cnt and any(str(getattr(op, "nid", "")) == str(cnt) for op in (trade.operations or [])):
                    return lid
        return None

    def _sdk_event(self, market: str, ev) -> None:
        try:
            lid = self._find_local(market, ev)
            task, status = _name(getattr(ev, "task", None)), _name(getattr(ev, "status", None))
            msg = str(getattr(ev, "msg", "") or "")
            base = {"local_id": lid, "market": market, "task": task, "status": status, "msg": msg,
                    "order_id": getattr(ev, "order_id", None), "symbol": getattr(ev, "symbol", None)}
            if task == "Deal":
                kind = "deal"
            elif task == "NewOrder":
                if status in ("Failed",) or status in US_FAIL_STATUS:
                    kind = "reject"
                elif status in ("Success",) or status in US_OK_STATUS:
                    kind = "ack"
                else:
                    kind = "pending"
            elif task == "CancelOrder":
                kind = "cancelled" if status in ("Success",) or status in US_OK_STATUS else (
                    "cancel_failed" if status == "Failed" or status in US_FAIL_STATUS else "pending")
            else:
                kind = "info"
            base.update(kind=kind, qty=int(_num(getattr(ev, "quantity", 0), 0)),
                        price=_num(getattr(ev, "price", None), None))
            self._on_event(base)
        except Exception as e:  # noqa: BLE001  回呼不可丟例外回 SDK
            self._log("error", f"處理回報失敗：{e}")


class LocalSim:
    """本機模擬成交（不送券商）：
    - 模擬模式的美股（凱基沒有複委託模擬環境）
    - 演練模式（--dry-run）的台股與美股：用正式帳號登入讀真實行情，但委託只在本機成交
    規則：限價單在最新價穿越限價時以限價成交（價格更有利則用最新價）；市價、漲停、跌停、平盤單以下一筆最新價成交；
    IOC／FOK 限價單若當下無法成交就取消。不模擬部分成交、排隊與漲跌停鎖死，結果會比實際樂觀。
    """

    def __init__(self, emit: Callable[[dict], None]):
        self.emit = emit
        self.orders: dict[str, dict] = {}
        self.pos: dict[str, int] = {}
        self.last: dict[tuple, float] = {}
        self._ids = itertools.count(1)

    @staticmethod
    def _key(spec: OrderSpec) -> tuple:
        return (spec.market, spec.symbol, spec.unit in ("odd", "odd_after"))

    def has(self, lid: str) -> bool:
        return lid in self.orders

    def place(self, lid: str, spec: OrderSpec) -> None:
        self.orders[lid] = {"spec": spec, "open": True, "oid": f"SIM{next(self._ids):05d}"}
        self.emit({"local_id": lid, "market": spec.market, "kind": "ack", "task": "NewOrder", "status": "Success",
                   "order_id": self.orders[lid]["oid"], "qty": spec.qty, "price": spec.price, "msg": "本機模擬"})
        k = self._key(spec)
        if k in self.last:
            self._try(lid, self.last[k])
        if self.orders[lid]["open"] and spec.tif in ("IOC", "FOK") and spec.price_type == "LMT":
            self.cancel(lid)

    def cancel(self, lid: str) -> tuple[bool, str]:
        o = self.orders.get(lid)
        if not o or not o["open"]:
            return False, "委託已成交或已取消"
        o["open"] = False
        self.emit({"local_id": lid, "market": o["spec"].market, "kind": "cancelled", "task": "CancelOrder",
                   "status": "Success", "qty": 0, "price": None, "msg": "本機模擬"})
        return True, ""

    def on_tick(self, market: str, sym: str, odd: bool, price: float) -> None:
        self.last[(market, sym, odd)] = price
        for lid, o in list(self.orders.items()):
            if o["open"] and self._key(o["spec"]) == (market, sym, odd):
                self._try(lid, price)

    def _try(self, lid: str, price: float) -> None:
        o = self.orders[lid]
        s = o["spec"]
        if s.price_type != "LMT":
            fill = price
        elif (s.action == "Buy" and price <= s.price) or (s.action == "Sell" and price >= s.price):
            fill = min(price, s.price) if s.action == "Buy" else max(price, s.price)
        else:
            return
        o["open"] = False
        if s.market == "US":
            self.pos[s.symbol] = self.pos.get(s.symbol, 0) + (s.qty if s.action == "Buy" else -s.qty)
        self.emit({"local_id": lid, "market": s.market, "kind": "deal", "task": "Deal", "status": "",
                   "order_id": o["oid"], "qty": s.qty, "price": fill, "msg": "本機模擬成交",
                   "ts": datetime.now().isoformat(timespec="seconds")})

    def inventory(self) -> list[dict]:
        return [{"market": "US", "symbol": s, "name": "", "kind": "cash", "shares": q, "avg": None,
                 "last": self.last.get(("US", s, False))} for s, q in self.pos.items() if q > 0]
