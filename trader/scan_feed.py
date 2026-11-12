"""自動下載最新的選股掃描結果（stage_scan.json）。

GitHub Actions 每個交易日收盤後掃描並發布到 GitHub Pages；下單程式啟動時下載一次，
之後每 15 分鐘檢查是否有新版（用 ETag，沒更新時不重新下載整個檔案）。
下載的檔案存在 trader/runtime/stage_scan.json，網頁從本機 /stage_scan.json 讀取；
網路中斷時沿用上次下載的版本。
"""

from __future__ import annotations

import json
import os
import threading
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable

from .market_hours import TPE

DEFAULT_URL = "https://royaleo1207.github.io/stage-scan/stage_scan.json"
CHECK_EVERY_SEC = 15 * 60


def _http_get(url: str, etag: str | None, timeout: float = 30) -> tuple[int, bytes, str | None]:
    req = urllib.request.Request(url, headers={"User-Agent": "stage-scan-trader"})
    if etag:
        req.add_header("If-None-Match", etag)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), r.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, b"", etag
        raise


class ScanFeed:
    def __init__(self, folder: Path, url: str = DEFAULT_URL,
                 fetch: Callable[[str, str | None], tuple[int, bytes, str | None]] | None = None,
                 log: Callable[..., object] | None = None):
        self.path = Path(folder) / "stage_scan.json"
        self.meta_path = Path(folder) / "stage_scan.meta.json"
        self.url = url
        self.fetch = fetch or _http_get
        self.log = log or (lambda *a, **k: None)
        self.lock = threading.Lock()
        self.meta = {"generated_at": None, "fetched_at": None, "checked_at": None, "etag": None, "error": None}
        try:
            self.meta.update(json.loads(self.meta_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass

    def info(self) -> dict:
        with self.lock:
            return {k: v for k, v in self.meta.items() if k != "etag"} | {"available": self.path.exists(),
                                                                           "url": self.url}

    def read_bytes(self) -> bytes | None:
        try:
            return self.path.read_bytes()
        except OSError:
            return None

    def refresh(self) -> bool:
        """下載最新版；有新資料回傳 True。失敗時保留舊檔並記錄錯誤。"""
        now = datetime.now(TPE).isoformat(timespec="seconds")
        etag = self.meta.get("etag") if self.path.exists() else None
        with self.lock:
            self.meta["error"] = None       # 下載中：畫面顯示「正在下載」
        try:
            status, body, new_etag = self.fetch(self.url, etag)
        except Exception as e:  # noqa: BLE001  網路錯誤不影響下單
            with self.lock:
                self.meta.update(checked_at=now, error=f"下載選股資料失敗：{e}")
            self._save_meta()
            return False
        changed = False
        with self.lock:
            self.meta.update(checked_at=now, error=None)
            if status == 200 and body:
                try:
                    data = json.loads(body.decode("utf-8"))
                    if not isinstance(data, dict) or not isinstance(data.get("stocks"), list) or not data["stocks"]:
                        raise ValueError("內容沒有 stocks")
                except ValueError as e:
                    self.meta["error"] = f"選股資料格式錯誤：{e}"
                else:
                    gen = data.get("generated_at")
                    changed = gen != self.meta.get("generated_at") or not self.path.exists()
                    tmp = self.path.with_suffix(".tmp")
                    tmp.write_bytes(body)
                    os.replace(tmp, self.path)
                    self.meta.update(generated_at=gen, fetched_at=now, etag=new_etag)
        self._save_meta()
        if changed:
            self.log("scan", f"已下載最新選股資料（掃描時間 {self.meta['generated_at']}）")
        return changed

    def _save_meta(self) -> None:
        try:
            self.meta_path.write_text(json.dumps(self.meta, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def run(self, stop: threading.Event, every: float = CHECK_EVERY_SEC) -> None:
        while not stop.is_set():
            self.refresh()
            stop.wait(every)
