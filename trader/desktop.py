"""桌面版：獨立視窗、沒有黑色命令視窗。

雙擊專案資料夾的 StageScanTrader.pyw 啟動（.pyw 由 pythonw.exe 執行，不會出現命令視窗）。
- 視窗使用 pywebview（Windows 內建的 Edge WebView2），沒安裝時改用預設瀏覽器開啟。
- 原本印在黑色視窗的訊息（含凱基 SuperPy 的訊息）寫到 trader/runtime/console.log。
- 已經在執行時再雙擊，只會開啟視窗連到原本的程式，不會啟動第二個。
- 關閉視窗或按「結束程式」會結束下單程式（部位與出場規則已存檔，下次登入同一模式接續監控）。
- 啟動失敗（例如設定檔有誤）會跳出 Windows 訊息框說明原因。
"""

from __future__ import annotations

import os
import sys
import threading
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TITLE = "階段掃描下單"
CLOSE_TEXT = "關閉視窗會結束下單程式，並停止監控停損停利。\n（部位不會賣出，下次登入同一模式會接續監控）\n\n確定要關閉嗎？"
MAX_LOG = 5 * 1024 * 1024


def message_box(text: str, title: str = TITLE, error: bool = True) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x10 if error else 0x40)
    except (AttributeError, OSError):
        print(f"[{title}] {text}", file=sys.__stderr__ or sys.stderr)


def redirect_output(runtime: Path) -> Path:
    """沒有命令視窗時，把輸出寫到 console.log（超過 5MB 時改名為 console.old.log）。"""
    runtime.mkdir(parents=True, exist_ok=True)
    log = runtime / "console.log"
    try:
        if log.exists() and log.stat().st_size > MAX_LOG:
            os.replace(log, runtime / "console.old.log")
    except OSError:
        pass
    f = open(log, "a", encoding="utf-8", buffering=1, errors="replace")
    sys.stdout = sys.stderr = f
    return log


def open_window(url: str, svc=None, storage: Path | None = None) -> bool:
    """開啟獨立視窗，視窗關閉後才回傳。沒有 pywebview 時回傳 False。"""
    try:
        import webview
    except ImportError:
        return False
    try:
        webview.settings["ALLOW_DOWNLOADS"] = True          # 「匯出計畫 JSON」需要
    except (AttributeError, TypeError):
        pass
    win = webview.create_window(TITLE, url, width=1320, height=900, min_size=(960, 640), confirm_close=svc is not None)
    if svc is not None:
        svc.quit_hooks.append(win.destroy)
    kw = {"localization": {"global.quitConfirmation": CLOSE_TEXT, "global.ok": "確定", "global.cancel": "取消"}}
    if storage is not None:
        kw.update(private_mode=False, storage_path=str(storage))   # 保留下單夾等瀏覽器資料
    webview.start(**kw)
    return True


def main() -> int:
    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from trader.order_server import PORT, Services, existing_instance
    from trader.store import RUNTIME

    console = None
    if sys.stdout is None or "pythonw" in os.path.basename(sys.executable).lower():
        console = redirect_output(RUNTIME)
    print(f"===== 啟動 {TITLE} =====")

    if existing_instance(PORT):                              # 已經在執行：只開視窗
        url = f"http://127.0.0.1:{PORT}/"
        if not open_window(url):
            import webbrowser
            webbrowser.open(url)
        return 0
    try:
        svc = Services(PORT)
    except OSError as e:
        message_box(f"無法啟動下單程式：連接埠 {PORT} 被其他程式佔用，或網路設定有問題。\n\n{e}")
        return 2
    except ValueError as e:
        message_box(f"設定有誤，請修改 trader\\order_config.yaml：\n\n{e}")
        return 3
    except Exception as e:  # noqa: BLE001
        message_box(f"下單程式啟動失敗：{e}\n\n詳細內容在 {console or '終端機'}")
        traceback.print_exc()
        return 1
    svc.start_background(serve=True)
    try:
        shown = open_window(svc.url, svc, RUNTIME / "webview")
    except Exception as e:  # noqa: BLE001  WebView2 有問題時改用瀏覽器
        traceback.print_exc()
        shown = False
        message_box(f"無法開啟程式視窗（{e}），改用瀏覽器開啟。\n要結束請按畫面右上角「結束程式」。", error=False)
    if not shown:
        import webbrowser
        webbrowser.open(svc.url)
        if console is not None and "webview" not in sys.modules:
            message_box("尚未安裝視窗元件 pywebview，改用瀏覽器開啟。\n要結束請按畫面右上角「結束程式」。\n\n"
                        "安裝方式：在命令提示字元執行  python -m pip install pywebview", error=False)
        svc.stop.wait()                                       # 等「結束程式」
    svc.shutdown()
    print(f"===== {TITLE} 已結束 =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
