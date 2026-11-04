"""從 stage_scan.json 產生當天的台股觀察名單。"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

TPE = timezone(timedelta(hours=8))


def load_scan(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def watchlist(scan: dict, cfg: dict, today: datetime | None = None) -> tuple[list[dict], list[str]]:
    """回傳 (觀察名單, 警告)。名單依 RS 由高到低，最多 max_watchlist 檔。"""
    uni, warn = cfg["universe"], []
    gen = scan.get("generated_at")
    today = (today or datetime.now(TPE)).astimezone(TPE).date()
    if not gen:
        warn.append("掃描資料沒有產生時間，視為過期：今天不開新倉。")
        return [], warn
    gdate = datetime.fromisoformat(gen).astimezone(TPE).date()
    if gdate != today:
        warn.append(f"掃描資料是 {gdate} 產生的，不是今天（{today}）：今天不開新倉。")
        return [], warn
    if scan.get("demo"):
        warn.append("這是示範資料，不能用來交易。")
        return [], warn

    rows = []
    for s in scan.get("stocks", []):
        if s.get("market") != "TW":
            continue
        if s.get("stage") not in uni["stages"]:
            continue
        if uni["trend_template"] and not all(s.get("tt") or [False]):
            continue
        if (s.get("rs") or 0) < uni["min_rs"]:
            continue
        if (s.get("avg_vol50") or 0) < uni["min_avg_volume_lots"] * 1000:
            continue
        rows.append({"symbol": s["ticker"].split(".")[0], "ticker": s["ticker"], "name": s.get("name", ""),
                     "stage": s["stage"], "rs": s.get("rs"), "close": s.get("close"),
                     "avg_vol50": s.get("avg_vol50")})
    rows.sort(key=lambda r: r["rs"] or 0, reverse=True)
    if len(rows) > cfg["max_watchlist"]:
        warn.append(f"符合條件 {len(rows)} 檔，依 RS 只取前 {cfg['max_watchlist']} 檔（報價訂閱上限）。")
        rows = rows[:cfg["max_watchlist"]]
    return rows, warn
