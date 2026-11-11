"""測試用的假 SuperPy（kgisuperpy）模組。

列舉值與 kgisuperpy 2.1.2 的 trading/_trade_base.py 相同；api 物件只實作下單程式用到的方法，
並記錄每一次呼叫，讓測試檢查送出的參數是否正確，也可以手動觸發委託回報與 tick。
"""

from __future__ import annotations

import itertools
from enum import Enum
from types import SimpleNamespace


class Action(Enum):
    Buy = "B"
    Sell = "S"


class TimeInForce(Enum):
    ROD = 0
    IOC = 1
    FOK = 2


class PriceType(Enum):
    LMT = 0
    MKT = "1"
    LimitDown = 1
    LimitUp = 9
    Reference = 5
    StopLossMarket = "2"
    StopLossLimit = "3"
    RangeMarket = "4"


class OddLot(Enum):
    Common = 0
    Fixing = 2
    Odd_AfterMarket = 1
    Odd = 4


class OrderCond(Enum):
    CASH = 0
    MARGIN = 3
    SHORT_SELLING = 4
    Lend_SELLING = 6
    MARGIN_DayTrade = 7
    SHORT_DayTrade = 8
    CASH_SELLING = 9


class Currency(Enum):
    TWD = 0
    MUT = 1


class Task(Enum):
    NewOrder = "NewOrder"
    CancelOrder = "CancelOrder"
    Deal = "Deal"


class Status(Enum):
    Pending = "Pending"
    Success = "Success"
    Failed = "Failed"


def contract(sym, day_trade="Yes", bull=110.0, bear=90.0, ref=100.0):
    return SimpleNamespace(symbol=sym, name=f"測試{sym}", day_trade=day_trade, bull_limit=bull,
                           bear_limit=bear, ref_price=ref)


class _Order:
    def __init__(self, api):
        self.api, self.calls, self.cancels, self.cb = api, [], [], None
        self._ids = itertools.count(1)
        self.contracts = {"2330": contract("2330"), "2317": contract("2317", day_trade="OnlyBuy"),
                          "1101": contract("1101", day_trade="No")}
        self.credit = {"MarignQty": "50", "ShortQty": "30", "ErrCode": "0", "ErrMsg": ""}
        self.trades = {}

    def set_event(self, cb):
        self.cb = cb

    def contract(self, type="dic"):
        return self.contracts

    def TSECreditInfo(self, symbol):  # noqa: N802
        return [dict(self.credit, StockNo=symbol)]

    def create_order(self, action, symbol, qty, price=None, time_in_force=TimeInForce.ROD,
                     order_cond=OrderCond.CASH, odd_lot=False, name=""):
        if price == PriceType.RangeMarket:
            raise ValueError("現股PriceType僅支持 MKT、LimitUp、LimitDown、Reference，或直接輸入價格 ")
        if order_cond == OrderCond.CASH_SELLING and action == Action.Buy:
            raise ValueError("action 和order_cond發生衝突")
        oid = f"T{next(self._ids):04d}"
        call = dict(action=action, symbol=symbol, qty=qty, price=price, time_in_force=time_in_force,
                    order_cond=order_cond, odd_lot=odd_lot, name=name, order_id=oid)
        self.calls.append(call)
        trade = SimpleNamespace(order=SimpleNamespace(order_id=oid, org_seqnum=None, symbol=symbol, action=action),
                                order_status=SimpleNamespace(status=Status.Pending, deals=[]), operations=[])
        self.trades[oid] = trade
        return trade

    def cancel_order(self, order_id):
        self.cancels.append(order_id)

    def get_trades(self, full=False):
        return self.trades

    # 測試用：觸發回報
    def emit(self, task, order_id, qty=0, price=None, status=None, msg=""):
        self.cb(SimpleNamespace(task=task, status=status, order_id=order_id, seqno=None, quantity=qty,
                                price=price, msg=msg, symbol=None))


class _Account:
    def __init__(self):
        self.rows = []

    def InventorySum(self, FType="A"):  # noqa: N802,N803
        return self.rows


class _SubOrder:
    def __init__(self):
        self.calls, self.cancels, self.cb = [], [], None
        self._ids = itertools.count(1)
        self.contracts = {"AAPL.O": SimpleNamespace(market="US", board_lot=1),
                          "0700.HK": SimpleNamespace(market="HK", board_lot=100)}
        self.trades = {}

    def set_event(self, cb):
        self.cb = cb

    def contract(self, type="dic"):
        return self.contracts

    def create_order(self, action, symbol, qty, price, currency=Currency.MUT, name=""):
        c = self.contracts[symbol]
        if c.market != "US":
            raise ValueError("目前僅支援 US 市場 (Market.US)")
        seq = f"S{next(self._ids):04d}"
        self.calls.append(dict(action=action, symbol=symbol, qty=qty, price=price, currency=currency, org_seqnum=seq))
        trade = SimpleNamespace(order=SimpleNamespace(order_id=None, org_seqnum=seq),
                                order_status=SimpleNamespace(status=Status.Pending, deals=[]), operations=[])
        self.trades[seq] = trade
        return trade

    def cancel_order(self, org_seq):
        self.cancels.append(org_seq)

    def get_trades(self, full=False):
        return self.trades

    def emit(self, task, org_seqnum, qty=0, price=None, status=None):
        self.cb(SimpleNamespace(task=task, status=status, order_id=None, org_seqnum=org_seqnum, seqno=None,
                                quantity=qty, price=price, msg="", symbol=None))


class _SubAccount:
    def __init__(self):
        self.rows = []

    def StockPositionReport(self):  # noqa: N802
        return self.rows


def _check_cb(func):
    """與 kgisuperpy 2.1.2 basic_quote.__set_and_check_callback_function__ 相同的檢查。"""
    if not callable(func) or func.__annotations__.get("return") not in (None, type(None)):
        raise TypeError("The provided function does not match the required signature.")
    if len(func.__annotations__) > 2:
        raise TypeError("The provided function must only accept self and one additional parameter.")


class _Quote:
    def __init__(self):
        self.cb, self.subs = None, []

    def set_cb_tick(self, cb):
        _check_cb(cb)
        self.cb = cb

    def subscribe_tick(self, symbol, odd_lot=False):
        self.subs.append((symbol, odd_lot))

    def push(self, symbol, close, odd=False):
        self.cb(SimpleNamespace(symbol=symbol, close=close, odd_lot=odd, datetime=""))


class _USQuote(_Quote):
    def subscribe_tick(self, symbol):
        self.subs.append((symbol, False))


class FakeApi:
    def __init__(self, simulation):
        self.simulation = simulation
        self.Order = _Order(self)
        self.Account = _Account()
        self.SubOrder = _SubOrder()
        self.SubAccount = _SubAccount()
        self.Quote = _Quote()
        self.USQuote = _USQuote()
        self.selected = []

    def show_account(self):
        return [{"account": "9801234", "broker_id": "9A00", "account_flag": "證券"},
                {"account": "9805678", "broker_id": "9A00", "account_flag": "複委託"}]

    def set_Account(self, a):  # noqa: N802
        self.selected.append(("證券", a))
        self._old_trades = self.Order.trades

    def set_SubAccount(self, a):  # noqa: N802
        self.selected.append(("複委託", a))

    def set_disconnect_cb(self, f):
        self.disconnect_cb = f

    def login(self):            # 官方重登範例：在斷線回呼內呼叫既有物件的 api.login()
        self.relogins = getattr(self, "relogins", 0) + 1
        self.Order = _Order(self)       # 重新登入後 Order 物件重建、舊的 callback 失效
        self.Order.trades = self._old_trades
        self.Quote.subs.clear()
        self.USQuote.subs.clear()

    def logout(self):
        pass


class FakeKgi(SimpleNamespace):
    """假的 kgisuperpy 模組。"""

    def __init__(self):
        super().__init__(Action=Action, TimeInForce=TimeInForce, PriceType=PriceType, OddLot=OddLot,
                         OrderCond=OrderCond, Currency=Currency, Task=Task, Status=Status)
        self.api = None
        self.login_args = None

    def login(self, pid, pwd, simulation=True):
        self.login_args = (pid, simulation)
        self.api = FakeApi(simulation)
        return self.api
