"""風控：單筆金額、單日總金額、單日最大虧損、空方部位總金額。

金額一律換算成台幣比較（美股以 usd_twd 匯率換算）。
平倉單（停損停利、強制平倉、緊急平倉）永遠不受金額上限阻擋。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RiskLimits:
    max_order_amount: float = 300_000        # 單筆金額上限（台幣）
    max_daily_amount: float = 2_000_000      # 單日新委託總金額上限（台幣）
    max_daily_loss: float = 30_000           # 單日最大虧損（台幣，已實現＋未實現）
    max_short_amount: float = 500_000        # 空方部位總金額上限（台幣）
    max_orders_per_day: int = 50
    usd_twd: float = 32.0

    @classmethod
    def from_cfg(cls, d: dict) -> "RiskLimits":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


class RiskManager:
    def __init__(self, limits: RiskLimits):
        self.limits = limits

    def to_twd(self, market: str, amount: float) -> float:
        return amount * (self.limits.usd_twd if market == "US" else 1.0)

    def check_new_order(self, market: str, amount: float, is_short_open: bool, day: dict,
                        short_exposure_twd: float) -> str | None:
        """回傳拒絕原因；可以下單回傳 None。day：當日統計（amount、orders、halted）。"""
        L = self.limits
        if day.get("halted"):
            return f"今日已停止新委託：{day.get('halt_reason') or '觸發風控'}"
        amt = self.to_twd(market, amount)
        if amt > L.max_order_amount:
            return f"預估金額 {amt:,.0f} 元超過單筆上限 {L.max_order_amount:,.0f} 元"
        if day.get("amount", 0) + amt > L.max_daily_amount:
            return (f"今日已委託 {day.get('amount', 0):,.0f} 元，加上這筆 {amt:,.0f} 元"
                    f"會超過單日上限 {L.max_daily_amount:,.0f} 元")
        if day.get("orders", 0) + 1 > L.max_orders_per_day:
            return f"今日委託筆數已達上限 {L.max_orders_per_day} 筆"
        if is_short_open and short_exposure_twd + amt > L.max_short_amount:
            return (f"空方部位 {short_exposure_twd:,.0f} 元加上這筆 {amt:,.0f} 元"
                    f"會超過空方總金額上限 {L.max_short_amount:,.0f} 元")
        return None

    def loss_breached(self, pnl_twd: float) -> bool:
        return pnl_twd <= -abs(self.limits.max_daily_loss)
