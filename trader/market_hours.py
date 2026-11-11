"""台股與美股（複委託）的交易時段。

- 台股依台北時間：盤中整股 09:00–13:30（08:30 起可預先委託）、盤中零股 09:00–13:30、
  盤後定價 14:00–14:30、盤後零股 13:40–14:30。
- 美股依美東時間 09:30–16:00，用 zoneinfo 換算，自動處理夏令（EDT, UTC−4）／冬令（EST, UTC−5）。
- 國定假日、美國休市日與提早收盤日沒有內建（需要時以 holidays 參數傳入）。

Windows 的 Python 沒有內建時區資料庫，需要 `pip install tzdata`（已列在 requirements）。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TPE = ZoneInfo("Asia/Taipei")
NY = ZoneInfo("America/New_York")

# 台股：單位 → (可委託起, 迄)
TW_ORDER_WINDOW = {
    "common": (time(8, 30), time(13, 30)),
    "odd": (time(9, 0), time(13, 30)),
    "odd_after": (time(13, 40), time(14, 30)),
    "fixing": (time(14, 0), time(14, 30)),
}
TW_OPEN, TW_CLOSE = time(9, 0), time(13, 30)
TW_DAYTRADE_FLATTEN = time(13, 20)          # 當沖預設強制平倉時間
US_OPEN, US_CLOSE = time(9, 30), time(16, 0)
US_FLATTEN_BEFORE_CLOSE = timedelta(minutes=15)


def now_tpe() -> datetime:
    return datetime.now(TPE)


def _is_weekday(d: date, holidays: set[date] | None) -> bool:
    return d.weekday() < 5 and not (holidays and d in holidays)


def tw_can_order(unit: str, now: datetime, holidays: set[date] | None = None) -> tuple[bool, str]:
    t = now.astimezone(TPE)
    if not _is_weekday(t.date(), holidays):
        return False, "台股今天休市"
    lo, hi = TW_ORDER_WINDOW.get(unit, TW_ORDER_WINDOW["common"])
    if lo <= t.time() <= hi:
        return True, ""
    return False, f"不在台股{ {'common': '整股', 'odd': '盤中零股', 'odd_after': '盤後零股', 'fixing': '盤後定價'}.get(unit, '')}" \
                  f"委託時段（{lo:%H:%M}–{hi:%H:%M}，台北時間）"


def tw_in_session(now: datetime, holidays: set[date] | None = None) -> bool:
    t = now.astimezone(TPE)
    return _is_weekday(t.date(), holidays) and TW_OPEN <= t.time() <= TW_CLOSE


# 台股撮合時段（單位 → 起, 迄）：開盤前 08:30–09:00 只收單不成交
TW_MATCH_WINDOW = {
    "common": (time(9, 0), time(13, 30)),
    "odd": (time(9, 0), time(13, 30)),
    "odd_after": (time(13, 40), time(14, 30)),
    "fixing": (time(14, 0), time(14, 30)),
}


def can_match(market: str, unit: str, now: datetime, holidays: set[date] | None = None) -> bool:
    """這個時間是否為正規撮合時段（美股不含盤前盤後；凱基複委託在盤前盤後只收預約單）。"""
    if market == "US":
        return us_in_session(now, holidays)
    t = now.astimezone(TPE)
    if not _is_weekday(t.date(), holidays):
        return False
    lo, hi = TW_MATCH_WINDOW.get(unit, TW_MATCH_WINDOW["common"])
    return lo <= t.time() <= hi


def us_session(day: date) -> tuple[datetime, datetime]:
    """某個美東日期的開盤、收盤時間（帶時區）。"""
    return datetime.combine(day, US_OPEN, NY), datetime.combine(day, US_CLOSE, NY)


def us_in_session(now: datetime, holidays: set[date] | None = None) -> bool:
    t = now.astimezone(NY)
    if not _is_weekday(t.date(), holidays):
        return False
    o, c = us_session(t.date())
    return o <= t <= c


def us_flatten_time(now: datetime) -> datetime:
    """美股當沖強制平倉時間：當天（美東）收盤前 15 分鐘。"""
    d = now.astimezone(NY).date()
    return us_session(d)[1] - US_FLATTEN_BEFORE_CLOSE


def us_trade_date(now: datetime) -> date:
    return now.astimezone(NY).date()


def tw_trade_date(now: datetime) -> date:
    return now.astimezone(TPE).date()


def trade_date(market: str, now: datetime) -> date:
    return us_trade_date(now) if market == "US" else tw_trade_date(now)


def market_tz(market: str) -> ZoneInfo:
    return NY if market == "US" else TPE


def at_local(market: str, day: date, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(day, time(h, m), market_tz(market))


def default_flatten(market: str, now: datetime) -> datetime:
    if market == "US":
        return us_flatten_time(now)
    return datetime.combine(tw_trade_date(now), TW_DAYTRADE_FLATTEN, TPE)


def trading_days_between(start: date, end: date, holidays: set[date] | None = None) -> int:
    """start（不含）到 end（含）之間的交易日數。"""
    n, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if _is_weekday(d, holidays):
            n += 1
    return n


def utc_offset_hours(market: str, now: datetime) -> float:
    return now.astimezone(market_tz(market)).utcoffset().total_seconds() / 3600
