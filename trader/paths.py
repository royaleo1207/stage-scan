"""使用者資料放在哪裡。

- 從原始碼執行（下載 ZIP）：放在專案的 trader/ 資料夾（runtime/、.env、order_config.yaml），跟以前一樣。
- 安裝版：安裝程式會在程式資料夾放一個 INSTALLED 檔，資料改放在
  %LOCALAPPDATA%\\StageScanTrader，更新或重新安裝都不會被覆蓋。
- 也可以用環境變數 STAGE_SCAN_DATA 指定資料夾。
"""

from __future__ import annotations

import os
from pathlib import Path

TRADER_DIR = Path(__file__).resolve().parent
ROOT = TRADER_DIR.parent


def data_dir() -> Path:
    env = os.environ.get("STAGE_SCAN_DATA")
    if env:
        return Path(env)
    if (ROOT / "INSTALLED").exists():
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "StageScanTrader"
    return TRADER_DIR


DATA = data_dir()
RUNTIME = DATA / "runtime"
