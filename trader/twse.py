"""台股現股交易規則：升降單位、漲跌停價、交易成本。"""

from __future__ import annotations

import math
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

SHARES_PER_LOT = 1000

# (價格下限, 每檔)；由高到低比對
_TICKS = ((1000, Decimal("5")), (500, Decimal("1")), (100, Decimal("0.5")),
          (50, Decimal("0.1")), (10, Decimal("0.05")), (0, Decimal("0.01")))


def tick_size(price: float) -> float:
    for floor, t in _TICKS:
        if price >= floor:
            return float(t)
    return 0.01


def _tick_dec(price: Decimal) -> Decimal:
    for floor, t in _TICKS:
        if price >= floor:
            return t
    return Decimal("0.01")


def round_to_tick(price: float, direction: str = "nearest") -> float:
    """把價格對齊到合法檔位。direction：up、down、nearest。"""
    p = Decimal(str(price))
    t = _tick_dec(p)
    q = p / t
    if direction == "up":
        q = q.to_integral_value(ROUND_CEILING)
    elif direction == "down":
        q = q.to_integral_value(ROUND_FLOOR)
    else:
        q = q.to_integral_value()
    # 各級距的分界（10、50、100、500、1000）都是上一級檔位的整數倍，
    # 所以以原級距檔位取整後一定仍是合法價格
    return float(q * t)


def ticks_away(price: float, n: int) -> float:
    """往上（n>0）或往下（n<0）移動 n 檔。"""
    p = Decimal(str(round_to_tick(price)))
    for _ in range(abs(n)):
        if n > 0:
            p += _tick_dec(p)
        else:
            # 往下一檔用「下方」價位的檔位，例如 10.00 往下是 9.99
            p -= _tick_dec(p - Decimal("0.001"))
    return float(p)


def limit_prices(prev_close: float, pct: float = 0.10) -> tuple[float, float]:
    """漲停價、跌停價（參考價 ±10%，漲停向下取檔、跌停向上取檔）。"""
    p, k = Decimal(str(prev_close)), Decimal(str(pct))
    return (round_to_tick(float(p * (1 + k)), "down"), round_to_tick(float(p * (1 - k)), "up"))


def trade_cost(amount: float, side: str, costs: dict, day_trade: bool) -> float:
    """單邊成本（元）。side：buy 或 sell。手續費兩邊都收；證交稅只在賣出收。"""
    fee = amount * costs["fee_rate"] * costs["fee_discount"]
    fee = max(costs["min_fee"], math.floor(fee)) if amount > 0 else 0.0
    tax = 0.0
    if side == "sell":
        tax = math.floor(amount * (costs["tax_day_trade"] if day_trade else costs["tax_rate"]))
    return float(fee + tax)
