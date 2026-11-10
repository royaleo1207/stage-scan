"""委託單規格：選項、限制與平倉對應（台股與複委託共用的單一依據）。

所有選項都以 kgisuperpy 2.1.2（PyPI 最新版）的實際原始碼為準：
- 台股 api.Order.create_order(action, symbol, qty, price, time_in_force, order_cond, odd_lot, name)
  - OrderCond：CASH、MARGIN、SHORT_SELLING、Lend_SELLING、CASH_SELLING（現股先賣），
    另有 MARGIN_DayTrade／SHORT_DayTrade 只出現在回報物件，手冊未列為可下單參數，不使用。
  - PriceType：MKT、LimitUp、LimitDown、Reference，或直接給數字（限價）；
    RangeMarket（範圍市價）傳入會直接 raise「現股PriceType僅支持 MKT、LimitUp、LimitDown、Reference」。
  - TimeInForce：ROD、IOC、FOK。
  - OddLot：Common（整股）、Odd（盤中零股）、Odd_AfterMarket（盤後零股）、Fixing（盤後定價）。
  - CASH_SELLING 搭配 Buy 會 raise「action 和order_cond發生衝突」，所以先賣後買的回補用 CASH 買進。
- 複委託 api.SubOrder.create_order(action, symbol, qty, price, currency=Currency.MUT, name)
  - 只有限價、ROD（docstring「建立一筆委託單（限價、ROD 單）」），沒有 time_in_force／order_cond／odd_lot 參數。
  - 只接受 Market.US，qty 必須是 board_lot 的倍數。
  - 凱基手冊明載複委託沒有模擬環境。

這個檔案同時提供給網頁（GET /api/capabilities），讓網頁停用 API 不支援的選項並顯示原因。
"""

from __future__ import annotations

from dataclasses import dataclass

# ── 顯示名稱 ─────────────────────────────────────────
CATEGORY_LABEL = {
    "cash": "現股",
    "margin": "融資",
    "short": "融券",
    "lend_sell": "借券賣出",
    "collateral_sell": "賣出擔保品",
    "daytrade_short": "現股當沖先賣",
}
UNIT_LABEL = {
    "common": "整股（張）",
    "odd": "盤中零股（股）",
    "odd_after": "盤後零股（股）",
    "fixing": "定盤／盤後定價（張）",
    "shares": "股數",
}
PRICE_TYPE_LABEL = {
    "LMT": "限價",
    "MKT": "市價",
    "RANGE_MKT": "範圍市價",
    "LIMIT_UP": "漲停",
    "LIMIT_DOWN": "跌停",
    "REFERENCE": "平盤",
}
TIF = ("ROD", "IOC", "FOK")

# ── API 不支援的選項（網頁停用，並顯示原因） ─────────────
UNSUPPORTED = {
    "TW": {
        "category": {
            "collateral_sell": "SuperPy 2.1.2 的 OrderCond 沒有「賣出擔保品」（只有 CASH、MARGIN、SHORT_SELLING、"
                               "Lend_SELLING、CASH_SELLING），API 無法下這種單。",
        },
        "price_type": {
            "RANGE_MKT": "SuperPy 台股 create_order 傳入 PriceType.RangeMarket 會直接報錯"
                         "（現股僅支援市價、漲停、跌停、平盤與限價）。",
        },
        "unit": {
            "shares": "台股用整股／盤中零股／盤後零股／定盤，不用「股數」單位。",
        },
    },
    "US": {
        "category": {
            "margin": "複委託 SubOrder.create_order 沒有 order_cond 參數，無法融資。",
            "short": "複委託 SubOrder.create_order 沒有 order_cond 參數，無法融券放空。",
            "lend_sell": "複委託不支援借券賣出。",
            "collateral_sell": "複委託不支援賣出擔保品。",
            "daytrade_short": "複委託不支援先賣後買（只能賣出既有庫存）。",
        },
        "price_type": {
            "MKT": "複委託 API 只接受限價單（SubOrder.create_order 說明：限價、ROD 單）。",
            "RANGE_MKT": "複委託 API 只接受限價單。",
            "LIMIT_UP": "美股沒有漲跌停，複委託 API 只接受限價單。",
            "LIMIT_DOWN": "美股沒有漲跌停，複委託 API 只接受限價單。",
            "REFERENCE": "複委託 API 只接受限價單。",
        },
        "tif": {
            "IOC": "複委託 SubOrder.create_order 沒有 time_in_force 參數，固定為 ROD。",
            "FOK": "複委託 SubOrder.create_order 沒有 time_in_force 參數，固定為 ROD。",
        },
        "unit": {
            "common": "美股以股數下單。", "odd": "美股以股數下單。",
            "odd_after": "美股以股數下單。", "fixing": "美股以股數下單。",
        },
    },
}

# 交易所規則（API 收得下，但送出後會被交易所或券商退單，所以預先擋下）
EXCHANGE_RULES = {
    "odd_limit_rod": "盤中零股、盤後零股、盤後定價只接受限價 ROD（交易所規則）。",
    "credit_common_only": "融資、融券、借券賣出、現股當沖先賣只能用整股（交易所規則）。",
    "daytrade_common_only": "當沖只能用盤中整股（零股與盤後交易不能當沖）。",
    "daytrade_short_daytrade": "現股當沖先賣必須當日買回，部位類型固定為當沖。",
}

# 開倉組合：(買賣別, 交易類別) → 部位方向
OPENING = {
    ("Buy", "cash"): "long",
    ("Buy", "margin"): "long",
    ("Sell", "short"): "short",
    ("Sell", "lend_sell"): "short",
    ("Sell", "daytrade_short"): "short",
}
# 平倉組合（賣出庫存）：(買賣別, 交易類別) → 庫存種類
CLOSING = {
    ("Sell", "cash"): "cash",
    ("Sell", "margin"): "margin",
    ("Buy", "short"): "short",
}

# 部位種類 → 平倉方式（買賣別, 交易類別, 說明）
CLOSE_METHOD = {
    "cash": ("Sell", "cash", "現股賣出"),
    "margin": ("Sell", "margin", "融資賣出"),
    "short": ("Buy", "short", "融券買回"),
    "daytrade_short": ("Buy", "cash", "先賣後買的當日買回（現股買進）"),
    "lend_sell": ("Buy", "cash", "現股買回（借券需另行還券）"),
}


@dataclass
class OrderSpec:
    market: str             # TW / US
    symbol: str
    action: str             # Buy / Sell
    category: str           # cash / margin / short / lend_sell / collateral_sell / daytrade_short
    unit: str               # common / odd / odd_after / fixing / shares
    price_type: str         # LMT / MKT / RANGE_MKT / LIMIT_UP / LIMIT_DOWN / REFERENCE
    tif: str                # ROD / IOC / FOK
    qty: int
    price: float | None = None
    currency: str = "MUT"   # 複委託交割幣別：TWD（台幣）/ MUT（外幣）
    name: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "OrderSpec":
        return cls(
            market=str(d.get("market", "")).upper(),
            symbol=str(d.get("symbol", "")).strip().upper(),
            action=str(d.get("action", "")),
            category=str(d.get("category", "cash")),
            unit=str(d.get("unit", "common" if str(d.get("market", "")).upper() == "TW" else "shares")),
            price_type=str(d.get("price_type", "LMT")),
            tif=str(d.get("tif", "ROD")),
            qty=d.get("qty"),
            price=d.get("price"),
            currency=str(d.get("currency", "MUT")),
            name=str(d.get("name", ""))[:40],
        )

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def shares_of(spec_or_unit, qty: int | None = None) -> int:
    """委託數量換算成股數（整股、定盤為張；零股、美股為股）。"""
    if isinstance(spec_or_unit, OrderSpec):
        unit, qty = spec_or_unit.unit, spec_or_unit.qty
    else:
        unit = spec_or_unit
    return int(qty) * (1000 if unit in ("common", "fixing") else 1)


def position_side(action: str, category: str) -> str | None:
    """開倉單的部位方向（long／short）；不是開倉單回傳 None。"""
    return OPENING.get((action, category))


def is_closing(action: str, category: str) -> bool:
    return (action, category) in CLOSING


def position_kind(action: str, category: str) -> str:
    """開倉單成交後的部位種類，用來決定平倉方式。"""
    return {"cash": "cash", "margin": "margin", "short": "short",
            "lend_sell": "lend_sell", "daytrade_short": "daytrade_short"}[category]


def close_method(kind: str) -> tuple[str, str, str]:
    return CLOSE_METHOD[kind]


def exit_unit(entry_unit: str) -> str:
    """出場時的單位：整股、定盤成交的部位用整股出；零股（含盤後零股）用盤中零股出；美股用股數。"""
    if entry_unit in ("common", "fixing"):
        return "common"
    if entry_unit in ("odd", "odd_after"):
        return "odd"
    return "shares"


def validate_spec(spec: OrderSpec) -> list[str]:
    """只檢查選項本身（API 支援與交易所規則），不含行情、庫存與風控。回傳錯誤訊息清單。"""
    errs: list[str] = []
    m = spec.market
    if m not in ("TW", "US"):
        return [f"不認得的市場：{m}"]
    if not spec.symbol:
        errs.append("缺少商品代號")
    if spec.action not in ("Buy", "Sell"):
        errs.append(f"買賣別只能是買進或賣出，收到「{spec.action}」")
    if spec.category not in CATEGORY_LABEL:
        errs.append(f"不認得的交易類別：{spec.category}")
    if spec.unit not in UNIT_LABEL:
        errs.append(f"不認得的單位：{spec.unit}")
    if spec.price_type not in PRICE_TYPE_LABEL:
        errs.append(f"不認得的價格類型：{spec.price_type}")
    if spec.tif not in TIF:
        errs.append(f"不認得的委託條件：{spec.tif}")
    if errs:
        return errs

    un = UNSUPPORTED[m]
    for field, val in (("category", spec.category), ("price_type", spec.price_type),
                       ("tif", spec.tif), ("unit", spec.unit)):
        if val in un.get(field, {}):
            errs.append(un[field][val])

    if not isinstance(spec.qty, int) or isinstance(spec.qty, bool) or spec.qty < 1:
        errs.append("數量需為正整數")
    if spec.price_type == "LMT":
        if not isinstance(spec.price, (int, float)) or isinstance(spec.price, bool) or spec.price <= 0:
            errs.append("限價單需填價格")

    if m == "TW":
        if not errs and (spec.action, spec.category) not in OPENING and (spec.action, spec.category) not in CLOSING:
            pair = f"{'買進' if spec.action == 'Buy' else '賣出'}＋{CATEGORY_LABEL[spec.category]}"
            hint = {
                ("Sell", "lend_sell"): "", ("Buy", "lend_sell"): "借券賣出的回補請用「買進＋現股」。",
                ("Buy", "daytrade_short"): "先賣後買的回補請用「買進＋現股」（API 不接受 CASH_SELLING 買進）。",
            }.get((spec.action, spec.category), "")
            errs.append(f"「{pair}」不是有效的組合。{hint}")
        if spec.unit in ("odd", "odd_after", "fixing") and (spec.price_type != "LMT" or spec.tif != "ROD"):
            errs.append(EXCHANGE_RULES["odd_limit_rod"])
        if spec.category != "cash" and spec.unit != "common":
            errs.append(EXCHANGE_RULES["credit_common_only"])
    else:
        if spec.currency not in ("TWD", "MUT"):
            errs.append("交割幣別只能是 TWD（台幣）或 MUT（外幣）")
    return errs


def capabilities() -> dict:
    """給網頁用：所有選項、API 不支援的選項與原因、交易所規則。"""
    return {
        "sdk": "kgisuperpy 2.1.2",
        "labels": {"category": CATEGORY_LABEL, "unit": UNIT_LABEL, "price_type": PRICE_TYPE_LABEL, "tif": list(TIF)},
        "unsupported": UNSUPPORTED,
        "exchange_rules": EXCHANGE_RULES,
        "close_method": {k: v[2] for k, v in CLOSE_METHOD.items()},
        "notes": {
            "US_simulation": "凱基手冊明載複委託沒有模擬環境；模擬模式下美股委託由本機程式依即時報價模擬成交，不會送到券商。",
            "US_session": "美股依美東時間 09:30–16:00（自動處理夏令／冬令時間）；美國休市日與提早收盤日未內建，請自行留意。",
        },
    }
