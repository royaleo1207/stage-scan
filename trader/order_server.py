"""本機下單程式：接收網頁送來的下單計畫，透過凱基 SuperPy 下單並監控出場規則。

啟動（Windows 命令提示字元，在專案資料夾）：
    python -m trader.order_server            模擬環境（預設，simulation=True）
    python -m trader.order_server --dry-run  演練：用正式帳號登入讀真實行情與庫存，但委託只在本機模擬成交
    python -m trader.order_server --live     正式環境，啟動時需輸入 YES 確認

- 只綁定 127.0.0.1:8765，外部電腦連不到。
- 每次啟動產生 6 位數配對碼（或用 .env 的 PAIR_CODE 固定），網頁送單時需帶 X-Pair-Code。
- 處理 CORS 與 Chrome 的 Private Network Access（Access-Control-Allow-Private-Network）。
- 狀態頁：http://127.0.0.1:8765/
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import yaml

from . import engine as eng
from . import kgi_spec
from .envfile import load_env
from .store import Store

HOST, PORT = "127.0.0.1", 8765
HERE = Path(__file__).resolve().parent
CFG_DEFAULTS = HERE / "order_defaults.yaml"
CFG_USER = HERE / "order_config.yaml"
STATUS_PAGE = HERE / "status_page.html"
MAX_BODY = 512 * 1024


def load_cfg(user_path: Path | None = None) -> dict:
    base = yaml.safe_load(CFG_DEFAULTS.read_text(encoding="utf-8")) or {}
    p = Path(user_path or CFG_USER)
    over = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}

    def merge(a, b, pre=""):
        out = dict(a)
        for k, v in (b or {}).items():
            if k not in a:
                raise ValueError(f"order_config.yaml 有不認得的項目：{pre}{k}")
            out[k] = merge(a[k], v, f"{pre}{k}.") if isinstance(a[k], dict) else v
        return out
    return merge(base, over)


def origin_allowed(origin: str | None, allowed: list[str]) -> bool:
    if not origin:
        return False
    if origin in allowed or "*" in allowed:
        return origin != "null" or "null" in allowed
    u = urlparse(origin)
    if u.scheme in ("http", "https") and u.hostname in ("127.0.0.1", "localhost"):
        return True
    return False


def make_handler(engine: eng.Engine, pair_code: str, allowed_origins: list[str]):
    class H(BaseHTTPRequestHandler):
        server_version = "stage-scan-trader/1.0"

        def log_message(self, fmt, *args):   # 不把每個請求印到終端機
            pass

        # ── 共用 ──
        def _cors(self):
            origin = self.headers.get("Origin")
            if origin_allowed(origin, allowed_origins):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Pair-Code")
                self.send_header("Access-Control-Max-Age", "600")
                if self.headers.get("Access-Control-Request-Private-Network") == "true":
                    self.send_header("Access-Control-Allow-Private-Network", "true")

        def _json(self, code: int, obj) -> None:
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self._cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self) -> bool:
            # 防 DNS rebinding：Host 只能是 127.0.0.1 或 localhost
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _origin_ok(self) -> bool:
            o = self.headers.get("Origin")
            return o is None or origin_allowed(o, allowed_origins)

        def _auth(self) -> bool:
            got = self.headers.get("X-Pair-Code", "")
            return hmac.compare_digest(got.encode(), pair_code.encode())

        def _guard(self, need_auth=True) -> bool:
            if not self._host_ok():
                self._json(403, {"ok": False, "error": "Host 不允許"})
                return False
            if not self._origin_ok():
                self._json(403, {"ok": False, "error": "來源網站不在允許清單（order_config.yaml 的 allowed_origins）"})
                return False
            if need_auth and not self._auth():
                engine.log("auth", f"配對碼錯誤：{self.command} {self.path} 來源 {self.headers.get('Origin')}")
                self._json(401, {"ok": False, "error": "配對碼錯誤，請輸入下單程式終端機顯示的配對碼"})
                return False
            return True

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("內容太大")
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                raise ValueError("Content-Type 需為 application/json")
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")

        # ── 路由 ──
        def do_OPTIONS(self):
            self.send_response(204 if (self._host_ok() and self._origin_ok()) else 403)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                if not self._host_ok():
                    return self._json(403, {"ok": False})
                body = STATUS_PAGE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                 "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/favicon.ico":           # 瀏覽器自動要圖示，不需配對碼也不寫紀錄
                self.send_response(204)
                self.end_headers()
                return
            if path == "/api/ping":
                if self._guard(need_auth=False):
                    self._json(200, {"ok": True, "app": "stage-scan-trader", "mode": engine.mode,
                                     "accounts": {"tw": engine.broker.has_tw, "us": engine.broker.has_us,
                                                  "us_local_sim": bool(getattr(engine.broker, "us_is_local_sim", False)),
                                                  "tw_local_sim": bool(getattr(engine.broker, "tw_is_local_sim", False))},
                                     "limits": engine.risk.limits.__dict__})
                return
            if path == "/api/capabilities":
                if self._guard(need_auth=False):
                    self._json(200, kgi_spec.capabilities())
                return
            if not self._guard():
                return
            if path == "/api/state":
                return self._json(200, engine.snapshot())
            if path == "/api/inventory":
                return self._json(200, engine.inventory())
            self._json(404, {"ok": False, "error": "沒有這個路徑"})

        def do_POST(self):
            path = urlparse(self.path).path
            if not self._guard():
                return
            try:
                body = self._body()
            except (ValueError, UnicodeDecodeError) as e:
                return self._json(400, {"ok": False, "error": f"內容格式錯誤：{e}"})
            if path == "/plan":
                return self._json(200, engine.submit_plan(body))
            if path == "/api/cancel_all":
                return self._json(200, engine.cancel_all())
            if path == "/api/flatten_all":
                return self._json(200, engine.flatten_all())
            if path == "/api/resume":
                return self._json(200, engine.resume())
            if path == "/api/cancel":
                ok, msg = engine.cancel_order(str(body.get("id")))
                return self._json(200, {"ok": ok, "error": msg})
            if path == "/api/detach":
                return self._json(200, engine.stop_monitor(str(body.get("id"))))
            self._json(404, {"ok": False, "error": "沒有這個路徑"})
    return H


def confirm_live(inp=input, out=print) -> bool:
    out("=" * 60)
    out("  ⚠  正式環境（simulation=False）：所有委託都會用真實資金送到凱基。")
    out("  請確認 README 的「正式啟用前檢查清單」都已完成。")
    out("=" * 60)
    try:
        return inp("確定要啟用正式下單，請輸入 YES：").strip() == "YES"
    except EOFError:
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="階段掃描本機下單程式（凱基 SuperPy）")
    ap.add_argument("--live", action="store_true", help="正式環境（預設為模擬環境 simulation=True）")
    ap.add_argument("--dry-run", action="store_true",
                    help="演練：用正式環境登入（讀真實行情、庫存），但所有委託只在本機模擬成交，不送到凱基")
    ap.add_argument("--port", type=int, default=PORT)
    a = ap.parse_args(argv)
    if a.live and a.dry_run:
        print("--live 與 --dry-run 只能擇一。")
        return 1

    load_env()
    if a.live and not confirm_live():
        print("未輸入 YES，已取消。")
        return 1
    cfg = load_cfg()
    store = Store()
    from .kgi_gateway import BrokerError, KgiBroker
    if a.dry_run:
        print("演練模式：以正式環境登入，只讀取行情、庫存與商品資料；委託一律在本機模擬成交，不會送到凱基。")
    broker = KgiBroker(simulation=not (a.live or a.dry_run), log=store.log, dry_run=a.dry_run)
    try:
        broker.login(os.environ.get("KGI_PERSON_ID", ""), os.environ.get("KGI_PERSON_PWD", ""),
                     os.environ.get("KGI_TW_ACCOUNT") or None, os.environ.get("KGI_US_ACCOUNT") or None)
    except BrokerError as e:
        print(f"[錯誤] {e}")
        return 2
    engine = eng.Engine(broker, store, cfg, live=a.live)
    label = "正式環境（真實下單）" if a.live else ("演練（正式登入，本機模擬成交，不送單）" if a.dry_run else "模擬環境")
    store.log("start", f"下單程式啟動：{label}")
    for n in engine.resume_after_restart():
        print("[接續]", n)

    pair = os.environ.get("PAIR_CODE") or f"{secrets.randbelow(10**6):06d}"
    srv = ThreadingHTTPServer((HOST, a.port), make_handler(engine, pair, cfg.get("allowed_origins", [])))
    stop = threading.Event()
    threading.Thread(target=eng.run_timer, args=(engine, stop), daemon=True).start()
    print("=" * 60)
    print(f"  模式：{label}")
    print(f"  台股帳號：{broker.tw_account or '未設定'}　複委託帳號：{broker.us_account or '未設定'}"
          + ("（模擬模式下美股由本機模擬成交）" if not (a.live or a.dry_run) else ""))
    print(f"  狀態頁：http://{HOST}:{a.port}/")
    print(f"  配對碼：{pair}")
    print("  按 Ctrl+C 結束（狀態已存檔，下次啟動會接續監控）")
    print("=" * 60)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.server_close()
        store.save()
        store.log("stop", "下單程式結束")
        broker.logout()
    return 0


if __name__ == "__main__":
    sys.exit(main())
