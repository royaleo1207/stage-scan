"""出場規則：停損、停利、移動停利、進場時間窗、突破價進場、出場時間、最長持有天數。

依部位方向自動判斷：
- 多方（現股買進、融資買進）：價格 ≤ 停損價出場、≥ 停利價出場；移動停利從最高價回落 X%。
- 空方（融券賣出、借券賣出、現股當沖先賣）：價格 ≥ 停損價出場、≤ 停利價出場；移動停利從最低價反彈 X%。

這裡只做判斷（純函式，方便測試），送單在 engine。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import market_hours as mh


@dataclass
class Level:
    type: str      # pct / price
    value: float

    @classmethod
    def parse(cls, d) -> "Level | None":
        if not d or d.get("value") in (None, ""):
            return None
        t = d.get("type", "pct")
        v = float(d["value"])
        if t not in ("pct", "price") or v <= 0:
            raise ValueError("停損／停利需為正數，類型為 % 或價格")
        return cls(t, v)


@dataclass
class ExitRules:
    position_type: str = "swing"                 # swing / daytrade
    stop_loss: Level | None = None
    take_profit: Level | None = None
    trailing_pct: float | None = None            # 從最有利價回落 X%
    entry_start: str | None = None               # 進場時間窗（市場當地時間 HH:MM）
    entry_end: str | None = None
    breakout_price: float | None = None          # 突破價進場
    exit_time: str | None = None                 # 當沖：平倉時間；波段：到期日的平倉時間
    max_hold_days: int | None = None             # 波段最長持有天數（交易日）

    @classmethod
    def from_dict(cls, d: dict | None) -> "ExitRules | None":
        if not d:
            return None
        pt = d.get("position_type", "swing")
        if pt not in ("swing", "daytrade"):
            raise ValueError("部位類型只能是波段或當沖")
        win = d.get("entry_window") or {}
        r = cls(
            position_type=pt,
            stop_loss=Level.parse(d.get("stop_loss")),
            take_profit=Level.parse(d.get("take_profit")),
            trailing_pct=_pos_or_none(d.get("trailing_pct"), "移動停利 %"),
            entry_start=_hhmm(win.get("start")),
            entry_end=_hhmm(win.get("end")),
            breakout_price=_pos_or_none(d.get("breakout_price"), "突破價"),
            exit_time=_hhmm(d.get("exit_time")),
            max_hold_days=None if d.get("max_hold_days") in (None, "") else int(d["max_hold_days"]),
        )
        if r.max_hold_days is not None and r.max_hold_days < 1:
            raise ValueError("最長持有天數至少 1 天")
        if (r.entry_start is None) != (r.entry_end is None):
            raise ValueError("進場時間窗需同時填開始與結束")
        if r.entry_start and r.entry_start >= r.entry_end:
            raise ValueError("進場時間窗的結束需晚於開始")
        if r.trailing_pct is not None and r.trailing_pct >= 100:
            raise ValueError("移動停利 % 需小於 100")
        for lv, nm in ((r.stop_loss, "停損"), (r.take_profit, "停利")):
            if lv and lv.type == "pct" and lv.value >= 100:
                raise ValueError(f"{nm} % 需小於 100")
        return r

    def to_dict(self) -> dict:
        return {
            "position_type": self.position_type,
            "stop_loss": self.stop_loss.__dict__ if self.stop_loss else None,
            "take_profit": self.take_profit.__dict__ if self.take_profit else None,
            "trailing_pct": self.trailing_pct,
            "entry_window": {"start": self.entry_start, "end": self.entry_end} if self.entry_start else None,
            "breakout_price": self.breakout_price,
            "exit_time": self.exit_time,
            "max_hold_days": self.max_hold_days,
        }

    def has_entry_condition(self) -> bool:
        return bool(self.entry_start or self.breakout_price)


def _pos_or_none(v, name):
    if v in (None, ""):
        return None
    v = float(v)
    if v <= 0:
        raise ValueError(f"{name}需為正數")
    return v


def _hhmm(v):
    if v in (None, ""):
        return None
    h, m = str(v).split(":")
    h, m = int(h), int(m)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"時間格式錯誤：{v}")
    return f"{h:02d}:{m:02d}"


def stop_price(rules: ExitRules, side: str, entry: float) -> float | None:
    lv = rules.stop_loss
    if not lv:
        return None
    if lv.type == "price":
        return lv.value
    return entry * (1 - lv.value / 100) if side == "long" else entry * (1 + lv.value / 100)


def target_price(rules: ExitRules, side: str, entry: float) -> float | None:
    lv = rules.take_profit
    if not lv:
        return None
    if lv.type == "price":
        return lv.value
    return entry * (1 + lv.value / 100) if side == "long" else entry * (1 - lv.value / 100)


def check_levels(rules: ExitRules, side: str, entry: float, ref: float | None) -> list[str]:
    """以價格設定的停損／停利是否在正確的一邊（以進場參考價檢查）。"""
    errs = []
    if ref is None or ref <= 0:
        return errs
    sl, tp = stop_price(rules, side, entry or ref), target_price(rules, side, entry or ref)
    if side == "long":
        if sl is not None and sl >= ref:
            errs.append(f"多方部位的停損價 {sl:g} 應低於進場價 {ref:g}")
        if tp is not None and tp <= ref:
            errs.append(f"多方部位的停利價 {tp:g} 應高於進場價 {ref:g}")
    else:
        if sl is not None and sl <= ref:
            errs.append(f"空方部位的停損價 {sl:g} 應高於進場價 {ref:g}")
        if tp is not None and tp >= ref:
            errs.append(f"空方部位的停利價 {tp:g} 應低於進場價 {ref:g}")
    return errs


@dataclass
class MonitorState:
    side: str                   # long / short
    entry: float                # 平均成交價
    best: float                 # 進場後最有利價（多方最高、空方最低）
    entry_date: str             # 市場當地日期 YYYY-MM-DD
    notes: list = field(default_factory=list)


def update_best(st: MonitorState, price: float) -> None:
    if st.side == "long":
        st.best = max(st.best, price)
    else:
        st.best = min(st.best, price)


def price_trigger(rules: ExitRules, st: MonitorState, price: float) -> str | None:
    """以最新成交價判斷是否觸發停損、停利或移動停利。回傳原因或 None。"""
    update_best(st, price)
    sl, tp = stop_price(rules, st.side, st.entry), target_price(rules, st.side, st.entry)
    if st.side == "long":
        if sl is not None and price <= sl:
            return f"停損（{price:g} ≤ {sl:.4g}）"
        if tp is not None and price >= tp:
            return f"停利（{price:g} ≥ {tp:.4g}）"
        if rules.trailing_pct and st.best > st.entry:
            line = st.best * (1 - rules.trailing_pct / 100)
            if price <= line:
                return f"移動停利（最高 {st.best:g} 回落 {rules.trailing_pct:g}%，{price:g} ≤ {line:.4g}）"
    else:
        if sl is not None and price >= sl:
            return f"停損（{price:g} ≥ {sl:.4g}）"
        if tp is not None and price <= tp:
            return f"停利（{price:g} ≤ {tp:.4g}）"
        if rules.trailing_pct and st.best < st.entry:
            line = st.best * (1 + rules.trailing_pct / 100)
            if price >= line:
                return f"移動停利（最低 {st.best:g} 反彈 {rules.trailing_pct:g}%，{price:g} ≥ {line:.4g}）"
    return None


def flatten_deadline(rules: ExitRules, market: str, entry_day: date, now: datetime,
                     holidays: set[date] | None = None) -> datetime | None:
    """時間出場的截止時刻（市場當地時間）：
    - 當沖：當天 exit_time；未填時台股 13:20、美股收盤前 15 分鐘。
    - 波段：設了最長持有天數時，在第 N 個交易日的 exit_time（未填同上）平倉；沒設則不以時間出場。
    """
    def at(day: date) -> datetime:
        if rules.exit_time:
            return mh.at_local(market, day, rules.exit_time)
        if market == "US":
            return mh.us_session(day)[1] - mh.US_FLATTEN_BEFORE_CLOSE
        return mh.at_local("TW", day, mh.TW_DAYTRADE_FLATTEN.strftime("%H:%M"))

    if rules.position_type == "daytrade":
        return at(entry_day)
    if rules.max_hold_days:
        d, n = entry_day, 0
        while n < rules.max_hold_days:
            d += timedelta(days=1)
            if d.weekday() < 5 and not (holidays and d in holidays):
                n += 1
        return at(d)
    return None


def time_trigger(rules: ExitRules, market: str, entry_day: date, now: datetime,
                 holidays: set[date] | None = None) -> str | None:
    dl = flatten_deadline(rules, market, entry_day, now, holidays)
    if dl is not None and now >= dl:
        if rules.position_type == "daytrade":
            return f"當沖時間平倉（{dl.astimezone(mh.market_tz(market)):%H:%M} {'美東' if market == 'US' else '台北'}時間）"
        return f"持有滿 {rules.max_hold_days} 個交易日"
    return None


def entry_window_state(rules: ExitRules, market: str, now: datetime) -> str:
    """before / open / closed（沒有時間窗時一律 open）。"""
    if not rules.entry_start:
        return "open"
    t = now.astimezone(mh.market_tz(market)).strftime("%H:%M")
    if t < rules.entry_start:
        return "before"
    if t > rules.entry_end:
        return "closed"
    return "open"


def breakout_hit(rules: ExitRules, side: str, price: float) -> bool:
    if not rules.breakout_price:
        return True
    return price >= rules.breakout_price if side == "long" else price <= rules.breakout_price
