"""本機下單程式測試（使用假的 SuperPy 物件，不會連線券商）。

執行：python tests/test_order_server.py（或 pytest tests/test_order_server.py）
"""
import http.client
import json
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fake_superpy import FakeKgi, OddLot, OrderCond, PriceType, Task, TimeInForce, Action, Status, Currency  # noqa: E402
from trader import exit_rules as xr  # noqa: E402
from trader import kgi_spec, market_hours as mh  # noqa: E402
from trader.engine import Engine  # noqa: E402
from trader.kgi_gateway import KgiBroker  # noqa: E402
from trader.order_server import confirm_live, make_handler, origin_allowed  # noqa: E402
from trader.store import Store  # noqa: E402


class Clock:
    def __init__(self, s, tz=mh.TPE):
        self.t = datetime.fromisoformat(s).replace(tzinfo=tz)

    def __call__(self):
        return self.t

    def set(self, s, tz=mh.TPE):
        self.t = datetime.fromisoformat(s).replace(tzinfo=tz)


def setup(now="2026-11-10T09:30:00", live=False, cfg=None, folder=None, us=True):
    kgi = FakeKgi()
    br = KgiBroker(kgi_module=kgi, simulation=not live)
    br.min_interval = 0      # 測試不等待下單間隔
    br.login("A123456789", "pw", "9801234", "9805678" if us else None)
    clock = Clock(now)
    store = Store(folder or tempfile.mkdtemp())
    e = Engine(br, store, cfg or {}, live=live, clock=clock)
    return e, kgi.api, clock


def tw(symbol="2330", action="Buy", category="cash", unit="common", qty=1, price=100.0, price_type="LMT",
       tif="ROD", exit=None, **kw):
    return dict(kind="order", market="TW", symbol=symbol, action=action, category=category, unit=unit,
                qty=qty, price=price, price_type=price_type, tif=tif, exit=exit, **kw)


def us(symbol="AAPL", action="Buy", qty=10, price=200.0, exit=None):
    return dict(kind="order", market="US", symbol=symbol, action=action, category="cash", unit="shares",
                qty=qty, price=price, price_type="LMT", tif="ROD", exit=exit, currency="MUT")


def plan(e, *items):
    r = e.submit_plan({"items": list(items)})
    assert r["ok"], r
    return r["results"]


def fill_last(api, qty=None, price=100.0):
    c = api.Order.calls[-1]
    api.Order.emit(Task.NewOrder, c["order_id"], status=Status.Success)
    api.Order.emit(Task.Deal, c["order_id"], qty=qty or c["qty"], price=price)
    return c


def only_pos(e):
    ps = [p for p in e.d["positions"].values() if p["status"] != "closed"]
    assert len(ps) == 1, ps
    return ps[0]


# ── 規格與 API 支援 ──
def test_unsupported_options_rejected():
    S = kgi_spec.OrderSpec.from_dict
    assert any("賣出擔保品" in m for m in kgi_spec.validate_spec(S(tw(action="Sell", category="collateral_sell"))))
    assert any("RangeMarket" in m for m in kgi_spec.validate_spec(S(tw(price_type="RANGE_MKT", price=None))))
    assert any("限價" in m for m in kgi_spec.validate_spec(S(dict(us(), price_type="MKT", price=None))))
    assert any("ROD" in m for m in kgi_spec.validate_spec(S(dict(us(), tif="IOC"))))
    assert any("融券" in m for m in kgi_spec.validate_spec(S(dict(us(), action="Sell", category="short"))))
    assert any("限價 ROD" in m for m in kgi_spec.validate_spec(S(tw(unit="odd", price_type="MKT", price=None))))
    assert any("整股" in m for m in kgi_spec.validate_spec(S(tw(category="margin", unit="odd"))))
    assert any("回補" in m for m in kgi_spec.validate_spec(S(tw(action="Buy", category="daytrade_short"))))
    assert kgi_spec.validate_spec(S(tw())) == []
    cap = kgi_spec.capabilities()
    assert "collateral_sell" in cap["unsupported"]["TW"]["category"]


def test_tw_order_maps_to_superpy_enums():
    e, api, _ = setup()
    plan(e, tw(price_type="MKT", price=None, tif="IOC"),
         tw(symbol="2317", action="Buy", category="margin", qty=2, price=100.0),
         tw(action="Sell", category="daytrade_short", qty=1, price_type="LIMIT_DOWN", price=None),
         tw(unit="odd", qty=50, price=101.0),
         tw(unit="fixing", qty=1, price=100.0))
    c = api.Order.calls
    assert c[0]["price"] == PriceType.MKT and c[0]["time_in_force"] == TimeInForce.IOC and c[0]["odd_lot"] == OddLot.Common
    assert c[1]["order_cond"] == OrderCond.MARGIN and c[1]["qty"] == 2
    assert c[2]["order_cond"] == OrderCond.CASH_SELLING and c[2]["action"] == Action.Sell
    assert c[2]["price"] == PriceType.LimitDown
    assert c[3]["odd_lot"] == OddLot.Odd and c[3]["qty"] == 50 and c[3]["price"] == 101.0
    assert c[4]["odd_lot"] == OddLot.Fixing


# ── 多方與空方：停損、停利、移動停利 ──
def test_long_stop_take_trailing():
    r = xr.ExitRules.from_dict({"stop_loss": {"type": "pct", "value": 2}, "take_profit": {"type": "pct", "value": 5},
                                "trailing_pct": 1})
    st = xr.MonitorState("long", 100, 100, "2026-11-10")
    assert xr.price_trigger(r, st, 99) is None
    assert xr.price_trigger(r, st, 98).startswith("停損")
    st = xr.MonitorState("long", 100, 100, "2026-11-10")
    assert xr.price_trigger(r, st, 105).startswith("停利")
    st = xr.MonitorState("long", 100, 100, "2026-11-10")
    assert xr.price_trigger(r, st, 103) is None and st.best == 103
    assert xr.price_trigger(r, st, 102.5) is None
    assert xr.price_trigger(r, st, 101.9).startswith("移動停利")


def test_short_stop_take_trailing():
    r = xr.ExitRules.from_dict({"stop_loss": {"type": "price", "value": 103}, "take_profit": {"type": "pct", "value": 4},
                                "trailing_pct": 1})
    st = xr.MonitorState("short", 100, 100, "2026-11-10")
    assert xr.price_trigger(r, st, 102.9) is None
    assert xr.price_trigger(r, st, 103).startswith("停損")
    st = xr.MonitorState("short", 100, 100, "2026-11-10")
    assert xr.price_trigger(r, st, 96).startswith("停利")
    st = xr.MonitorState("short", 100, 100, "2026-11-10")
    assert xr.price_trigger(r, st, 97) is None and st.best == 97
    assert xr.price_trigger(r, st, 98.0).startswith("移動停利")
    # 價格設定方向錯誤要被擋下
    assert xr.check_levels(r, "short", 104, 104)  # 停損 103 低於進場 104
    assert xr.check_levels(xr.ExitRules.from_dict({"stop_loss": {"type": "price", "value": 101}}), "long", 100, 100)


def test_engine_long_stop_loss_sends_cash_sell():
    e, api, _ = setup()
    plan(e, tw(qty=2, exit={"position_type": "swing", "stop_loss": {"type": "pct", "value": 2}}))
    fill_last(api, price=100.0)
    p = only_pos(e)
    assert p["side"] == "long" and p["shares"] == 2000
    api.Quote.push("2330", 99.0)
    assert len(api.Order.calls) == 1
    api.Quote.push("2330", 98.0)
    c = api.Order.calls[-1]
    assert c["action"] == Action.Sell and c["order_cond"] == OrderCond.CASH and c["qty"] == 2
    assert c["price"] == PriceType.LimitDown and c["odd_lot"] == OddLot.Common
    fill_last(api, price=98.0)
    p = e.d["positions"][p["id"]]
    assert p["status"] == "closed" and p["realized"] < -4000


def test_engine_short_take_profit_and_trailing():
    e, api, _ = setup()
    plan(e, tw(action="Sell", category="short", qty=1, price=100.0,
               exit={"take_profit": {"type": "pct", "value": 5}, "trailing_pct": 2}))
    fill_last(api, price=100.0)
    api.Quote.push("2330", 97.0)
    api.Quote.push("2330", 98.0)          # 從最低 97 反彈約 1%，未觸發
    assert len(api.Order.calls) == 1
    api.Quote.push("2330", 99.0)          # 反彈 2.06% 觸發移動停利
    c = api.Order.calls[-1]
    assert c["action"] == Action.Buy and c["order_cond"] == OrderCond.SHORT_SELLING and c["price"] == PriceType.LimitUp
    assert "移動停利" in only_pos(e)["exit_reason"]


# ── 各交易類別的平倉單 ──
def test_close_order_for_each_category():
    expect = {
        ("Buy", "cash"): (Action.Sell, OrderCond.CASH),
        ("Buy", "margin"): (Action.Sell, OrderCond.MARGIN),
        ("Sell", "short"): (Action.Buy, OrderCond.SHORT_SELLING),
        ("Sell", "daytrade_short"): (Action.Buy, OrderCond.CASH),
        ("Sell", "lend_sell"): (Action.Buy, OrderCond.CASH),
    }
    for (act, cat), (xa, xc) in expect.items():
        e, api, _ = setup()
        if cat == "lend_sell":
            api.Account.rows = [{"Symbol": "2330", "NETQTY5": "1000"}]
        pt = "daytrade" if cat == "daytrade_short" else "swing"
        plan(e, tw(action=act, category=cat, exit={"position_type": pt, "stop_loss": {"type": "pct", "value": 1}}))
        fill_last(api, price=100.0)
        api.Quote.push("2330", 102.0 if act == "Sell" else 98.0)
        c = api.Order.calls[-1]
        assert (c["action"], c["order_cond"]) == (xa, xc), (act, cat, c)
        assert len(api.Order.calls) == 2


def test_odd_lot_exit_uses_odd_limit():
    e, api, _ = setup()
    plan(e, tw(unit="odd", qty=300, price=100.0, exit={"stop_loss": {"type": "pct", "value": 3}}))
    fill_last(api, price=100.0)
    assert api.Quote.subs[-1] == ("2330", True)      # 零股部位訂閱零股行情
    api.Quote.push("2330", 96.0, odd=False)          # 整股行情不影響零股部位
    assert len(api.Order.calls) == 1
    api.Quote.push("2330", 96.0, odd=True)
    c = api.Order.calls[-1]
    assert c["odd_lot"] == OddLot.Odd and c["qty"] == 300 and isinstance(c["price"], float) and c["price"] < 96
    assert c["time_in_force"] == TimeInForce.ROD


def test_us_exit_is_limit_sell_via_suborder():
    e, api, _ = setup(now="2026-11-10T23:00:00", live=True)
    plan(e, us(qty=10, price=200.0, exit={"stop_loss": {"type": "pct", "value": 5}}))
    c = api.SubOrder.calls[-1]
    assert c["symbol"] == "AAPL.O" and c["currency"] == Currency.MUT and c["price"] == 200.0
    api.SubOrder.emit(Task.Deal, c["org_seqnum"], qty=10, price=200.0)
    api.USQuote.push("AAPL", 189.0)
    x = api.SubOrder.calls[-1]
    assert x["action"] == Action.Sell and x["qty"] == 10 and x["price"] == round(189.0 * 0.995, 2)


def test_us_rejects_non_us_market_and_unsellable():
    e, api, _ = setup(now="2026-11-10T23:00:00", live=True)
    r = e.submit_plan({"items": [us(symbol="0700"), us(action="Sell", qty=5)]})["results"]
    assert not r[0]["ok"] and "美國" in r[0]["reason"]
    assert not r[1]["ok"] and "庫存" in r[1]["reason"]


def test_us_simulation_uses_local_sim():
    e, api, _ = setup(now="2026-11-10T23:00:00")
    plan(e, us(qty=5, price=200.0, exit={"take_profit": {"type": "pct", "value": 2}}))
    assert api.SubOrder.calls == []                  # 凱基沒有複委託模擬環境，不送券商
    api.USQuote.push("AAPL", 199.0)
    p = only_pos(e)
    assert p["shares"] == 5 and p["avg"] == 199.0
    api.USQuote.push("AAPL", 203.0)
    api.USQuote.push("AAPL", 203.0)                  # 本機模擬讓出場限價單成交
    assert e.d["positions"][p["id"]]["status"] == "closed"


# ── 下單前檢查 ──
def test_prechecks():
    e, api, _ = setup()
    dt = {"position_type": "daytrade", "stop_loss": {"type": "pct", "value": 1}}
    api.Order.credit = {"MarignQty": "1", "ShortQty": "0", "ErrCode": "0", "ErrMsg": ""}
    r = e.submit_plan({"items": [
        tw(symbol="1101", exit=dt),                                   # 不可當沖
        tw(symbol="2317", action="Sell", category="daytrade_short", exit=dt),   # 只能先買後賣
        tw(category="margin", qty=2),                                 # 融資額度 1 張
        tw(action="Sell", category="short", qty=1),                   # 融券額度 0
        tw(action="Sell", category="cash", qty=1),                    # 沒有庫存
        tw(price=100.3),                                              # 檔位錯誤
        tw(price=120.0),                                              # 超過漲停
        tw(symbol="9999"),                                            # 查無商品
        tw(unit="odd", qty=10, exit=dt),                              # 零股不能當沖
        tw(action="Sell", category="cash", qty=1, exit={"stop_loss": {"type": "pct", "value": 1}}),  # 平倉單不可加出場規則
    ]})["results"]
    reasons = [x["reason"] for x in r]
    assert all(not x["ok"] for x in r), r
    assert "day_trade = No" in reasons[0] and "先賣後買" in reasons[1]
    assert "融資可用額度" in reasons[2] and "融券可用額度" in reasons[3]
    assert "庫存" in reasons[4] and "檔位" in reasons[5] and "漲跌停" in reasons[6] and "查無" in reasons[7]
    assert "零股" in reasons[8] and "開倉單" in reasons[9]
    assert api.Order.calls == []
    api.Account.rows = [{"Symbol": "2330", "NETQTY0": "2000"}]
    assert plan(e, tw(action="Sell", category="cash", qty=2))[0]["ok"]
    assert not plan(e, tw(action="Sell", category="cash", qty=1))[0]["ok"]   # 已被未成交賣單佔用


def test_live_hours_and_mode_mismatch():
    e, api, clock = setup(now="2026-11-10T07:00:00", live=True)
    r = plan(e, tw())
    assert not r[0]["ok"] and "委託時段" in r[0]["reason"]
    clock.set("2026-11-10T13:50:00")
    assert not plan(e, tw(unit="odd", qty=10))[0]["ok"]
    assert plan(e, tw(unit="odd_after", qty=10))[0]["ok"]
    r = e.submit_plan({"expected_mode": "simulation", "items": [tw()]})
    assert not r["ok"] and "模式" in r["error"]


def test_breakout_entry_and_window():
    e, api, clock = setup(now="2026-11-10T09:05:00")
    plan(e, tw(price_type="MKT", price=None, exit={"breakout_price": 105, "entry_window": {"start": "09:15", "end": "10:00"},
                                                    "stop_loss": {"type": "pct", "value": 2}}))
    api.Quote.push("2330", 106.0)                 # 時間窗還沒開始
    assert api.Order.calls == []
    clock.set("2026-11-10T09:20:00")
    api.Quote.push("2330", 104.0)                 # 未突破
    assert api.Order.calls == []
    api.Quote.push("2330", 105.5)
    assert len(api.Order.calls) == 1 and api.Order.calls[0]["price"] == PriceType.MKT
    # 第二筆：時間窗過了沒觸發就過期
    plan(e, tw(exit={"breakout_price": 120, "entry_window": {"start": "09:15", "end": "10:00"}}))
    clock.set("2026-11-10T10:01:00")
    e.on_timer()
    assert [o["status"] for o in e.d["orders"].values()][-1] == "expired"


# ── 當沖時間平倉 ──
def test_tw_daytrade_flatten_at_1320():
    e, api, clock = setup(now="2026-11-10T10:00:00")
    plan(e, tw(exit={"position_type": "daytrade"}))
    fill_last(api, price=100.0)
    clock.set("2026-11-10T13:19:00")
    e.on_timer()
    assert len(api.Order.calls) == 1
    clock.set("2026-11-10T13:20:00")
    e.on_timer()
    c = api.Order.calls[-1]
    assert c["action"] == Action.Sell and "當沖時間平倉" in only_pos(e)["exit_reason"]


def test_tw_daytrade_short_flatten_buys_back_with_cash():
    e, api, clock = setup(now="2026-11-10T10:00:00")
    plan(e, tw(action="Sell", category="daytrade_short", exit={"position_type": "daytrade", "exit_time": "13:00"}))
    fill_last(api, price=100.0)
    clock.set("2026-11-10T13:00:00")
    e.on_timer()
    c = api.Order.calls[-1]
    assert c["action"] == Action.Buy and c["order_cond"] == OrderCond.CASH and c["price"] == PriceType.LimitUp


def test_us_daytrade_flatten_dst_and_winter():
    # 夏令時間（EDT）：收盤 16:00 = 台北 04:00，平倉 03:45
    e, api, clock = setup(now="2026-07-14T22:00:00", live=True)
    plan(e, us(exit={"position_type": "daytrade"}))
    c = api.SubOrder.calls[-1]
    api.SubOrder.emit(Task.Deal, c["org_seqnum"], qty=10, price=200.0)
    api.USQuote.push("AAPL", 201.0)
    clock.set("2026-07-15T03:44:00")
    e.on_timer()
    assert len(api.SubOrder.calls) == 1
    clock.set("2026-07-15T03:45:00")
    e.on_timer()
    assert len(api.SubOrder.calls) == 2 and api.SubOrder.calls[-1]["action"] == Action.Sell
    # 冬令時間（EST）：收盤 16:00 = 台北 05:00，平倉 04:45
    e, api, clock = setup(now="2026-11-10T23:00:00", live=True)
    plan(e, us(exit={"position_type": "daytrade"}))
    c = api.SubOrder.calls[-1]
    api.SubOrder.emit(Task.Deal, c["org_seqnum"], qty=10, price=200.0)
    clock.set("2026-11-11T03:45:00")
    e.on_timer()
    assert len(api.SubOrder.calls) == 1
    clock.set("2026-11-11T04:45:00")
    e.on_timer()
    assert len(api.SubOrder.calls) == 2


def test_us_timezone_dst_boundaries():
    T = lambda s: datetime.fromisoformat(s).replace(tzinfo=mh.TPE)  # noqa: E731
    # 2026 夏令時間：3/8 開始、11/1 結束
    assert not mh.us_in_session(T("2026-03-06T22:00:00"))     # 3/6（五）冬令：開盤 22:30
    assert mh.us_in_session(T("2026-03-06T22:30:00"))
    assert mh.us_in_session(T("2026-03-09T21:30:00"))         # 3/9（一）夏令：開盤 21:30
    assert not mh.us_in_session(T("2026-03-09T21:29:00"))
    assert mh.us_in_session(T("2026-10-30T21:30:00"))         # 10/30 仍為夏令
    assert not mh.us_in_session(T("2026-11-02T21:30:00"))     # 11/2 冬令：開盤 22:30
    assert mh.us_in_session(T("2026-11-02T22:30:00"))
    assert not mh.us_in_session(T("2026-11-07T23:00:00"))     # 美東週六
    assert mh.us_trade_date(T("2026-11-11T04:00:00")).isoformat() == "2026-11-10"
    assert mh.utc_offset_hours("US", T("2026-07-01T00:00:00")) == -4
    assert mh.utc_offset_hours("US", T("2026-12-01T00:00:00")) == -5


def test_swing_max_hold_days():
    e, api, clock = setup(now="2026-11-12T10:00:00")      # 週四
    plan(e, tw(exit={"position_type": "swing", "max_hold_days": 2, "exit_time": "13:00"}))
    fill_last(api, price=100.0)
    clock.set("2026-11-13T13:30:00")
    e.on_timer()
    assert len(api.Order.calls) == 1
    clock.set("2026-11-16T12:59:00")                       # 第 2 個交易日（跳過週末）
    e.on_timer()
    assert len(api.Order.calls) == 1
    clock.set("2026-11-16T13:00:00")
    e.on_timer()
    assert len(api.Order.calls) == 2


# ── 風控 ──
def test_risk_limits():
    cfg = {"risk": {"max_order_amount": 150_000, "max_daily_amount": 250_000, "max_short_amount": 120_000}}
    e, api, _ = setup(cfg=cfg)
    r = e.submit_plan({"items": [tw(qty=2), tw(qty=1), tw(qty=1), tw(qty=1)]})["results"]
    assert "單筆上限" in r[0]["reason"]
    assert r[1]["ok"] and r[2]["ok"] and "單日上限" in r[3]["reason"]
    e, api, _ = setup(cfg=cfg)
    assert plan(e, tw(action="Sell", category="short", qty=1))[0]["ok"]
    r = plan(e, tw(action="Sell", category="short", qty=1))
    assert not r[0]["ok"] and "空方" in r[0]["reason"]
    r = plan(e, dict(us(qty=10, price=200.0)))        # 美股 2000 美元 × 32 = 64000 台幣
    assert r[0]["ok"]
    r = plan(e, dict(us(qty=30, price=200.0)))        # 6000 美元 × 32 = 192000 > 單筆 150000
    assert not r[0]["ok"] and "單筆" in r[0]["reason"]


def test_daily_loss_halts_and_flattens_daytrade():
    e, api, _ = setup(cfg={"risk": {"max_daily_loss": 5000}})
    plan(e, tw(qty=1, exit={"position_type": "daytrade"}), tw(symbol="2317", qty=1, exit={"position_type": "swing"}))
    api.Order.emit(Task.Deal, api.Order.calls[0]["order_id"], qty=1, price=100.0)
    api.Order.emit(Task.Deal, api.Order.calls[1]["order_id"], qty=1, price=100.0)
    api.Quote.push("2330", 97.0)
    assert not e.day()["halted"]
    api.Quote.push("2330", 95.0)                          # 2330 虧 5000
    assert e.day()["halted"] and "單日虧損" in e.day()["halt_reason"]
    c = api.Order.calls[-1]
    assert c["symbol"] == "2330" and c["action"] == Action.Sell     # 只平當沖部位
    assert len(api.Order.calls) == 3
    r = plan(e, tw(qty=1))
    assert not r[0]["ok"] and "停止新委託" in r[0]["reason"]
    assert not e.resume()["ok"]


def test_emergency_buttons():
    e, api, _ = setup()
    plan(e, tw(qty=1, exit={"stop_loss": {"type": "pct", "value": 5}}), tw(symbol="2317", qty=1),
         tw(exit={"breakout_price": 109}))
    api.Order.emit(Task.Deal, api.Order.calls[0]["order_id"], qty=1, price=100.0)
    r = e.cancel_all()
    assert r["cancelled"] == 2 and api.Order.cancels == [api.Order.calls[1]["order_id"]]
    assert [o["status"] for o in e.d["orders"].values()][2] == "cancelled"
    e.flatten_all()
    c = api.Order.calls[-1]
    assert c["action"] == Action.Sell and c["symbol"] == "2330" and e.day()["halted"]


def test_inventory_attach_exit():
    e, api, _ = setup()
    api.Account.rows = [{"Symbol": "2330", "SymbolName": "台積電", "NETQTY0": "3000", "AVG_PRICE0": "500",
                         "NETQTY3": "1000", "AVG_PRICE3": "480", "RLPRICE": "510"}]
    inv = e.inventory()["items"]
    assert {(r["kind"], r["shares"]) for r in inv} == {("cash", 3000), ("margin", 1000)}
    r = plan(e, {"kind": "position_exit", "market": "TW", "symbol": "2330",
                 "position": {"kind": "margin", "shares": 1000, "avg": 480},
                 "exit": {"stop_loss": {"type": "price", "value": 470}}})
    assert r[0]["ok"]
    api.Quote.push("2330", 469.0)
    c = api.Order.calls[-1]
    assert c["action"] == Action.Sell and c["order_cond"] == OrderCond.MARGIN and c["qty"] == 1
    r = plan(e, {"kind": "position_exit", "market": "TW", "symbol": "2330",
                 "position": {"kind": "cash", "shares": 5000, "avg": 500}, "exit": {"stop_loss": {"type": "pct", "value": 5}}})
    assert not r[0]["ok"] and "庫存只有" in r[0]["reason"]


def test_inventory_numbers_with_commas_and_raw_info():
    e, api, _ = setup()
    api.Account.rows = [{"Symbol": "2330 ", "NETQTY0": " 2,000 ", "AVG_PRICE0": "1,050.5", "RLPRICE": "1,060"}]
    j = e.inventory()
    assert [(r["symbol"], r["shares"], r["avg"], r["last"]) for r in j["items"]] == [("2330", 2000, 1050.5, 1060.0)]
    assert j["raw"]["TW"]["rows"] == 1 and "NETQTY0" in j["raw"]["TW"]["columns"]


def test_restart_resumes_monitoring():
    folder = tempfile.mkdtemp()
    e, api, _ = setup(folder=folder)
    plan(e, tw(qty=1, exit={"stop_loss": {"type": "pct", "value": 2}, "trailing_pct": 3}))
    fill_last(api, price=100.0)
    api.Quote.push("2330", 104.0)
    # 重啟：新的 broker 與 engine，讀同一個狀態檔
    e2, api2, _ = setup(folder=folder)
    notes = e2.resume_after_restart()
    assert ("2330", False) in api2.Quote.subs
    p = only_pos(e2)
    assert p["state"]["best"] == 104.0
    api2.Quote.push("2330", 100.8)                  # 從 104 回落 3.08%
    c = api2.Order.calls[-1]
    assert c["action"] == Action.Sell and "移動停利" in only_pos(e2)["exit_reason"]
    assert isinstance(notes, list)


def test_exit_retry_after_timeout_for_odd_lot():
    e, api, clock = setup(cfg={"exits": {"retry_sec": 10}})
    plan(e, tw(unit="odd", qty=100, price=100.0, exit={"stop_loss": {"type": "pct", "value": 2}}))
    fill_last(api, price=100.0)
    api.Quote.push("2330", 97.5, odd=True)
    first = api.Order.calls[-1]
    clock.set("2026-11-10T09:30:11")
    e.on_timer()
    assert api.Order.cancels == [first["order_id"]]
    api.Order.emit(Task.CancelOrder, first["order_id"], status=Status.Success)
    second = api.Order.calls[-1]
    assert second is not first and second["price"] < first["price"]


# ── 本機伺服器 ──
def test_server_pair_code_cors_and_pna():
    from http.server import ThreadingHTTPServer
    e, api, _ = setup()
    from trader.scan_feed import ScanFeed
    scan = ScanFeed(Path(tempfile.mkdtemp()), url="https://example.invalid/s.json",
                    fetch=lambda url, etag: (200, json.dumps({"generated_at": "G1", "stocks": [{"ticker": "2330.TW"}]}).encode(), "E1"))
    scan.refresh()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(e, "123456", ["https://royaleo1207.github.io"], scan))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def req(method, path, body=None, headers=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            h = {"Host": f"127.0.0.1:{port}", **(headers or {})}
            if body is not None:
                h["Content-Type"] = "application/json"
            c.request(method, path, json.dumps(body) if body is not None else None, h)
            r = c.getresponse()
            data = r.read()
            return r, (json.loads(data) if data and r.getheader("Content-Type", "").startswith("application/json") else data)

        r, _ = req("OPTIONS", "/plan", headers={"Origin": "https://royaleo1207.github.io",
                                                 "Access-Control-Request-Method": "POST",
                                                 "Access-Control-Request-Private-Network": "true"})
        assert r.status == 204
        assert r.getheader("Access-Control-Allow-Origin") == "https://royaleo1207.github.io"
        assert r.getheader("Access-Control-Allow-Private-Network") == "true"
        assert "X-Pair-Code" in r.getheader("Access-Control-Allow-Headers")
        r, _ = req("OPTIONS", "/plan", headers={"Origin": "https://evil.example"})
        assert r.status == 403 and r.getheader("Access-Control-Allow-Origin") is None
        r, j = req("GET", "/api/ping")
        assert r.status == 200 and j["mode"] == "simulation"
        r, j = req("POST", "/plan", {"items": [tw()]}, {"Origin": "https://royaleo1207.github.io"})
        assert r.status == 401 and api.Order.calls == []
        r, j = req("POST", "/plan", {"items": [tw()]}, {"Origin": "https://royaleo1207.github.io", "X-Pair-Code": "123456"})
        assert r.status == 200 and j["results"][0]["ok"] and len(api.Order.calls) == 1
        r, j = req("POST", "/plan", {"items": [tw()]}, {"Origin": "https://evil.example", "X-Pair-Code": "123456"})
        assert r.status == 403
        r, j = req("GET", "/api/state", headers={"Host": "attacker.example:8765", "X-Pair-Code": "123456"})
        assert r.status == 403
        r, j = req("GET", "/api/state", headers={"X-Pair-Code": "123456"})
        assert r.status == 200 and j["orders"]
        r, body = req("GET", "/")                              # 主畫面：自動帶入權杖，不用輸入配對碼
        html = body.decode("utf-8")
        assert r.status == 200 and '<meta name="trader-token" content="123456">' in html and "下單面板" in html
        assert r.getheader("X-Frame-Options") == "DENY" and r.getheader("Access-Control-Allow-Origin") is None
        r, body = req("GET", "/", headers={"Origin": "https://evil.example"})
        assert r.getheader("Access-Control-Allow-Origin") is None              # 其他網站讀不到權杖
        r, body = req("GET", "/status")
        assert r.status == 200 and "下單程式狀態" in body.decode("utf-8")
        r, j = req("GET", "/stage_scan.json")
        assert r.status == 200 and j["generated_at"] == "G1"
        r, j = req("GET", "/api/scan_info", headers={"X-Pair-Code": "123456"})
        assert j["generated_at"] == "G1" and j["available"]
        r, j = req("GET", "/api/scan_info")
        assert r.status == 401
    finally:
        srv.shutdown()
        srv.server_close()
    assert origin_allowed("http://localhost:5500", []) and not origin_allowed("null", [])


def test_live_requires_yes():
    assert confirm_live(lambda _: "YES", lambda *_: None)
    assert not confirm_live(lambda _: "yes", lambda *_: None)
    assert not confirm_live(lambda _: "", lambda *_: None)


def test_account_must_be_explicit():
    from trader.kgi_gateway import BrokerError
    br = KgiBroker(kgi_module=FakeKgi(), simulation=True)
    try:
        br.login("A1", "pw", None, None)
        raise AssertionError("應該要求指定帳號")
    except BrokerError as e:
        assert "不會自動選第一個帳號" in str(e)
    br = KgiBroker(kgi_module=FakeKgi(), simulation=True)
    try:
        br.login("A1", "pw", "9805678", None)       # 複委託帳號不能當證券帳號
        raise AssertionError
    except BrokerError as e:
        assert "不是證券帳號" in str(e)


def test_dry_run_logs_in_live_but_never_sends():
    """演練模式：正式環境登入（simulation=False），台股與美股委託都只在本機成交。"""
    kgi = FakeKgi()
    br = KgiBroker(kgi_module=kgi, simulation=False, dry_run=True)
    br.login("A1", "pw", "9801234", "9805678")
    assert kgi.login_args == ("A1", False)
    clock = Clock("2026-11-10T09:30:00")
    e = Engine(br, Store(tempfile.mkdtemp()), {}, live=False, clock=clock)
    assert e.mode == "dry_run"
    api = kgi.api
    plan(e, tw(qty=1, price=100.0, exit={"stop_loss": {"type": "pct", "value": 2}}),
         tw(symbol="2317", price_type="MKT", price=None, tif="IOC"))
    api.Quote.push("2330", 99.5)                      # 限價 100 成交於 99.5
    assert ("2317", False) in api.Quote.subs           # 沒加出場規則也要訂閱，否則本機無法模擬成交
    api.Quote.push("2317", 101.0)                     # 市價單以下一筆成交
    p = [x for x in e.d["positions"].values() if x["symbol"] == "2330"][0]
    assert p["avg"] == 99.5 and p["shares"] == 1000
    api.Quote.push("2330", 97.0)                      # 觸發停損，平倉單也在本機成交
    api.Quote.push("2330", 97.0)
    assert e.d["positions"][p["id"]]["status"] == "closed"
    clock.set("2026-11-10T23:00:00")
    plan(e, us(qty=2, price=200.0))
    assert ("AAPL", False) in api.USQuote.subs
    api.USQuote.push("AAPL", 199.0)
    assert api.Order.calls == [] and api.SubOrder.calls == []     # 從未送到凱基
    assert any(x["symbol"] == "AAPL" and x["status"] == "open" for x in e.d["positions"].values())
    # 演練也依真實交易時段檢查
    clock.set("2026-11-10T07:00:00")
    assert not plan(e, tw())[0]["ok"]
    # 重啟後本機模擬委託失效，不會被誤認為券商委託
    plan(e, us(qty=1, price=100.0))
    e2 = Engine(br, e.store, {}, live=False, clock=clock)
    e2.resume_after_restart()
    assert [o for o in e2.d["orders"].values()][-1]["status"] == "cancelled"


def test_reconnect_restores_callbacks_subscriptions_and_fills():
    e, api, _ = setup()
    plan(e, tw(qty=2, exit={"stop_loss": {"type": "pct", "value": 2}}))
    c = api.Order.calls[-1]
    api.Order.emit(Task.Deal, c["order_id"], qty=1, price=100.0)      # 斷線前成交 1 張
    br = e.broker
    br.RECONNECT_WAITS = (0,)
    # 斷線期間又成交 1 張（回報收不到），券商資料裡有
    from types import SimpleNamespace
    api.Order.trades[c["order_id"]].order_status.deals = [SimpleNamespace(quantity=1, price=100.0),
                                                          SimpleNamespace(quantity=1, price=100.0)]
    api.disconnect_cb()
    import time
    for _ in range(50):
        if getattr(api, "relogins", 0) and e.connected:
            break
        time.sleep(0.05)
    assert api.relogins == 1 and e.connected
    assert ("2330", False) in api.Quote.subs                          # 行情重新訂閱
    p = only_pos(e)
    assert p["shares"] == 2000                                        # 補上斷線期間的成交
    api.Quote.push("2330", 97.0)                                      # 新的 Order 物件也能收到平倉單
    assert api.Order.calls[-1]["action"] == Action.Sell and api.Order.calls[-1]["qty"] == 2


def test_disconnected_blocks_new_orders_and_defers_exit():
    e, api, _ = setup()
    plan(e, tw(qty=1, exit={"stop_loss": {"type": "pct", "value": 2}}))
    fill_last(api, price=100.0)
    e.on_broker_event({"kind": "disconnected", "local_id": None})
    r = e.submit_plan({"items": [tw()]})["results"][0]
    assert not r["ok"] and "連線中斷" in r["reason"]
    api.Quote.push("2330", 97.0)
    assert len(api.Order.calls) == 1 and only_pos(e)["status"] == "exit_waiting"
    e.connected = True
    e.on_timer()
    assert api.Order.calls[-1]["action"] == Action.Sell


def test_us_fees_in_realized_pnl():
    e, api, _ = setup(now="2026-11-10T23:00:00", live=True,
                      cfg={"us_costs": {"fee_rate": 0.0025, "min_fee": 15.0, "sec_fee_rate": 0.0}})
    plan(e, us(qty=10, price=200.0, exit={"take_profit": {"type": "pct", "value": 5}}))
    api.SubOrder.emit(Task.Deal, api.SubOrder.calls[-1]["org_seqnum"], qty=10, price=200.0)
    api.USQuote.push("AAPL", 210.0)
    api.SubOrder.emit(Task.Deal, api.SubOrder.calls[-1]["org_seqnum"], qty=10, price=210.0)
    p = [x for x in e.d["positions"].values()][0]
    # 價差 100 美元 − 買進手續費 15（2000×0.25%=5，未達最低 15）− 賣出手續費 15（2100×0.25%=5.25→15）＝ 70 美元 × 32
    assert p["status"] == "closed" and round(p["realized"]) == round((100 - 15 - 15) * 32)


def test_us_premarket_ticks_do_not_fill_or_trigger():
    """台北 17:10＝美東 04:10 盤前：凱基行情會推送盤前成交價，但不可成交、不可觸發。"""
    kgi = FakeKgi()
    br = KgiBroker(kgi_module=kgi, simulation=False, dry_run=True)
    br.login("A1", "pw", "9801234", "9805678")
    clock = Clock("2026-11-11T17:10:00")
    e = Engine(br, Store(tempfile.mkdtemp()), {}, live=False, clock=clock)
    api = kgi.api
    plan(e, us(qty=1, price=310.0), us(symbol="FDS", qty=1, price=330.0, exit={"breakout_price": 325}))
    api.USQuote.push("AAPL", 305.0)
    api.USQuote.push("FDS", 326.0)
    assert not e.d["positions"]
    assert [o["status"] for o in e.d["orders"].values()] == ["submitted", "armed"]
    clock.set("2026-11-11T22:31:00")                    # 開盤後
    api.USQuote.push("AAPL", 305.0)
    api.USQuote.push("FDS", 326.0)
    assert len(e.d["positions"]) == 2                   # 開盤後 AAPL 成交；FDS 突破、送出並成交
    # 持有中的部位：盤後價格不觸發停損
    plan(e, us(symbol="MSFT", qty=1, price=400.0, exit={"stop_loss": {"type": "pct", "value": 1}}))
    api.USQuote.push("MSFT", 400.0)
    clock.set("2026-11-12T05:30:00")                    # 美東 16:30 盤後
    api.USQuote.push("MSFT", 380.0)
    p = [x for x in e.d["positions"].values() if x["symbol"] == "MSFT"][0]
    assert p["status"] == "open"


def test_config_validation_catches_mistakes():
    from trader.order_server import load_cfg
    d = Path(tempfile.mkdtemp())
    assert load_cfg(d / "none.yaml")["costs"]["fee_discount"] == 1.0          # 預設值本身合法
    (d / "c.yaml").write_text("costs:\n  fee_discount: 0.28\nus_costs:\n  fee_rate: 0.001\n", encoding="utf-8")
    c = load_cfg(d / "c.yaml")
    assert c["costs"]["fee_discount"] == 0.28 and c["costs"]["fee_rate"] == 0.001425 and c["us_costs"]["fee_rate"] == 0.001
    for bad, word in (("costs:\n  fee_discount: 2.8\n", "0.28"), ("us_costs:\n  fee_rate: 0.25\n", "0.0025"),
                      ("costs:\n  fee_rate: abc\n", "需為數字"), ("costs:\n  feerate: 0.1\n", "不認得"),
                      ("costs：\n  fee_discount: 0.28\n", "格式錯誤")):
        (d / "b.yaml").write_text(bad, encoding="utf-8")
        try:
            load_cfg(d / "b.yaml")
            raise AssertionError(bad)
        except ValueError as e:
            assert word in str(e), (bad, str(e))


def test_scan_feed_downloads_and_keeps_last_good_copy():
    from trader.scan_feed import ScanFeed
    calls = []
    good = json.dumps({"generated_at": "2026-11-11T22:30:00+00:00", "stocks": [{"ticker": "AAPL"}]}).encode()
    replies = [(200, good, "E1"), (304, b"", "E1"), RuntimeError("網路中斷"), (200, "<html>維護中</html>".encode(), None),
               (200, good.replace(b"22:30", b"23:30"), "E2")]

    def fetch(url, etag):
        calls.append(etag)
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    d = Path(tempfile.mkdtemp())
    f = ScanFeed(d, url="https://example.invalid/s.json", fetch=fetch)
    assert f.refresh() is True and f.read_bytes() == good
    assert f.refresh() is False and calls[-1] == "E1"           # 沒更新：用 ETag，不重新下載
    assert f.refresh() is False and "網路中斷" in f.info()["error"] and f.read_bytes() == good
    assert f.refresh() is False and "格式錯誤" in f.info()["error"] and f.read_bytes() == good
    assert f.refresh() is True and "23:30" in f.info()["generated_at"] and f.info()["error"] is None
    g = ScanFeed(d, url="https://example.invalid/s.json", fetch=fetch)  # 重啟後記得上次的版本
    assert g.info()["generated_at"] == f.info()["generated_at"]


def test_rod_expires_after_close_and_pnl_rolls_over():
    kgi = FakeKgi()
    br = KgiBroker(kgi_module=kgi, simulation=False, dry_run=True)
    br.login("A1", "pw", "9801234", "9805678")
    clock = Clock("2026-11-11T21:39:00")                # 美東 08:39 盤前送出
    e = Engine(br, Store(tempfile.mkdtemp()), {}, live=False, clock=clock)
    api = kgi.api
    plan(e, us(qty=1, price=169.36), us(symbol="FDS", qty=1, price=330.0))
    clock.set("2026-11-11T23:00:00")
    api.USQuote.push("AAPL", 175.0)                     # 限價低於市價，不成交
    api.USQuote.push("FDS", 325.98)
    api.USQuote.push("FDS", 334.88)
    clock.set("2026-11-12T04:59:00")
    e.on_timer()
    assert list(e.d["orders"].values())[0]["status"] == "submitted"
    assert round(e.pnl_today()) == round((334.88 - 325.98) * 32)
    clock.set("2026-11-12T05:00:01")                    # 美東 16:00 收盤後
    e.on_timer()
    o = list(e.d["orders"].values())[0]
    assert o["status"] == "cancelled" and "ROD" in o["msg"]
    clock.set("2026-11-12T10:00:00")                    # 換日後還沒有新報價：今日損益為 0，不把昨天的漲幅算進今天
    assert round(e.pnl_today()) == 0
    clock.set("2026-11-12T22:31:00")
    api.USQuote.push("FDS", 336.88)                     # 今天漲 2 美元
    assert round(e.pnl_today()) == round(2 * 32)
    T = lambda x: datetime.fromisoformat(x).replace(tzinfo=mh.TPE)  # noqa: E731
    assert mh.session_close_after("TW", "common", T("2026-11-13T15:00:00")).isoformat().startswith("2026-11-16T13:30")
    assert mh.session_close_after("TW", "odd_after", T("2026-11-13T14:00:00")).isoformat().startswith("2026-11-13T14:30")


def test_cancelled_orders_release_daily_amount():
    e, api, _ = setup(cfg={"risk": {"max_daily_amount": 250_000}})
    plan(e, tw(qty=2, price=100.0))                      # 20 萬
    assert not plan(e, tw(qty=1, price=100.0))[0]["ok"]   # 再 10 萬超過 25 萬
    c = api.Order.calls[-1]
    api.Order.emit(Task.Deal, c["order_id"], qty=1, price=100.0)       # 成交 1 張
    e.cancel_order(list(e.d["orders"])[0])
    api.Order.emit(Task.CancelOrder, c["order_id"], status=Status.Success)
    assert e.day()["amount"] == 100_000                   # 未成交的 1 張退回額度
    assert plan(e, tw(qty=1, price=100.0))[0]["ok"]


def test_web_page_capabilities_match_server():
    """index.html 內嵌的選項表（未連線時用來停用選項）必須與 kgi_spec 一致。"""
    import re
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    m = re.search(r'<script type="application/json" id="caps">(.*?)</script>', html, re.S)
    assert m and json.loads(m.group(1)) == json.loads(json.dumps(kgi_spec.capabilities())), \
        "index.html 的 caps 與 trader/kgi_spec.py 不一致，請執行 python -m trader.sync_caps"


def test_gitignore_protects_secrets():
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "trader/.env" in gi and "trader/runtime/" in gi and "trader/order_config.yaml" in gi


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
