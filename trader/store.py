"""本機狀態檔（委託、成交、持倉、出場規則）與下單紀錄。

- 狀態：trader/runtime/state.json（每次變動後以「寫暫存檔再改名」的方式存檔，斷電也不會寫壞）
- 紀錄：trader/runtime/orders.log（一行一筆 JSON，只增不改）
runtime/ 已加入 .gitignore，不會提交。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

from .market_hours import TPE

from .paths import RUNTIME  # noqa: E402  安裝版放在 %LOCALAPPDATA%\\StageScanTrader

EMPTY = {"version": 1, "orders": {}, "fills": [], "positions": {}, "days": {}, "seq": 0}


class Store:
    def __init__(self, folder: Path | None = None):
        self.folder = Path(folder or RUNTIME)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / "state.json"
        self.log_path = self.folder / "orders.log"
        self._lock = threading.RLock()
        self.data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                for k, v in EMPTY.items():
                    d.setdefault(k, json.loads(json.dumps(v)))
                return d
            except (OSError, ValueError):
                bad = self.path.with_suffix(f".broken-{datetime.now():%Y%m%d%H%M%S}.json")
                self.path.rename(bad)
                self.log("error", f"狀態檔損毀，已改名為 {bad.name} 並重新開始")
        return json.loads(json.dumps(EMPTY))

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
            os.replace(tmp, self.path)

    def next_id(self, prefix: str = "L") -> str:
        with self._lock:
            self.data["seq"] += 1
            return f"{prefix}{datetime.now():%m%d}-{self.data['seq']:04d}"

    def log(self, kind: str, msg: str, **extra) -> dict:
        rec = {"ts": datetime.now(TPE).isoformat(timespec="seconds"), "kind": kind, "msg": msg, **extra}
        with self._lock:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return rec

    def tail_log(self, n: int = 200) -> list[dict]:
        if not self.log_path.exists():
            return []
        with open(self.log_path, encoding="utf-8") as f:
            lines = f.readlines()[-n:]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
        return out
