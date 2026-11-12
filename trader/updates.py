"""檢查 GitHub 上有沒有新版本的安裝程式（啟動時檢查一次，之後每 6 小時一次）。"""

from __future__ import annotations

import json
import re
import threading
import urllib.request

from .version import __version__

REPO = "royaleo1207/stage-scan"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
DOWNLOAD = f"https://github.com/{REPO}/releases/latest/download/StageScanTrader-Setup.exe"
RELEASES = f"https://github.com/{REPO}/releases/latest"


def parse(v: str) -> tuple[int, ...] | None:
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", (v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def _fetch_latest(timeout: float = 15) -> str | None:
    req = urllib.request.Request(API, headers={"User-Agent": "stage-scan-trader", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8")).get("tag_name")


class UpdateChecker:
    def __init__(self, current: str = __version__, fetch=_fetch_latest):
        self.current, self.fetch = current, fetch
        self.latest: str | None = None
        self.error: str | None = None

    def check(self) -> None:
        try:
            self.latest, self.error = self.fetch(), None
        except Exception as e:  # noqa: BLE001  網路錯誤不影響使用
            self.error = str(e)

    def info(self) -> dict:
        cur, new = parse(self.current), parse(self.latest or "")
        return {"current": self.current, "latest": self.latest,
                "newer": bool(cur and new and new > cur), "download": DOWNLOAD, "page": RELEASES}

    def run(self, stop: threading.Event, every: float = 6 * 3600) -> None:
        if parse(self.current) is None:          # 原始碼執行（dev）不檢查
            return
        while not stop.is_set():
            self.check()
            stop.wait(every)
