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
from .store import RUNTIME, Store

HOST, PORT = "127.0.0.1", 8765
HERE = Path(__file__).resolve().parent
CFG_DEFAULTS = HERE / "order_defaults.yaml"
CFG_USER = HERE / "order_config.yaml"
STATUS_PAGE = HERE / "status_page.html"
APP_PAGE = HERE.parent / "index.html"          # 與 GitHub Pages 同一份網頁；由本機提供時自動帶入權杖
TOKEN_META = '<meta name="trader-token" content="">'
MAX_BODY = 512 * 1024


# 設定值範圍（含端點）：填錯（例如手續費折扣 2.8 折填成 2.8）時啟動就停下來說明
CFG_RANGES = {
    "risk.max_order_amount": (1, None, "單筆金額上限（台幣）"),
    "risk.max_daily_amount": (1, None, "單日新委託總金額上限（台幣）"),
    "risk.max_daily_loss": (1, None, "單日最大虧損（台幣）"),
    "risk.max_short_amount": (0, None, "空方部位總金額上限（台幣）"),
    "risk.max_orders_per_day": (1, 10000, "單日委託筆數上限"),
    "risk.usd_twd": (10, 100, "美元兌台幣匯率，例如 31.5"),
    "exits.tw_odd_slip_ticks": (0, 50, "零股出場檔數"),
    "exits.us_slip_pct": (0, 10, "美股出場價差 %，例如 0.5"),
    "exits.retry_sec": (3, 600, "出場重掛秒數"),
    "exits.max_retries": (0, 50, "出場重送次數"),
    "costs.fee_rate": (0, 0.01, "台股手續費率，0.1425% 填 0.001425"),
    "costs.fee_discount": (0, 1, "台股手續費折扣，2.8 折填 0.28、6 折填 0.6、無折扣填 1.0"),
    "costs.min_fee": (0, 1000, "台股每筆最低手續費（元）"),
    "costs.tax_rate": (0, 0.01, "證交稅率，0.3% 填 0.003"),
    "costs.tax_day_trade": (0, 0.01, "當沖證交稅率，0.15% 填 0.0015"),
    "costs.slippage_ticks": (0, 20, "滑價檔數"),
    "us_costs.fee_rate": (0, 0.05, "美股手續費率，0.25% 填 0.0025"),
    "us_costs.min_fee": (0, 500, "美股每筆最低手續費（美元）"),
    "us_costs.sec_fee_rate": (0, 0.001, "SEC 規費率"),
}


def validate_cfg(cfg: dict) -> None:
    errs = []
    for key, (lo, hi, hint) in CFG_RANGES.items():
        sec, k = key.split(".")
        v = (cfg.get(sec) or {}).get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            errs.append(f"{key} 需為數字，收到 {v!r}（{hint}）")
        elif v < lo or (hi is not None and v > hi):
            rng = f"{lo}～{hi}" if hi is not None else f"至少 {lo}"
            errs.append(f"{key} = {v} 超出範圍 {rng}（{hint}）")
    if cfg.get("exits", {}).get("tw_common_price") not in ("limit_updown", "market"):
        errs.append("exits.tw_common_price 只能是 limit_updown 或 market")
    if not isinstance(cfg.get("enforce_hours_in_simulation"), bool):
        errs.append("enforce_hours_in_simulation 只能是 true 或 false")
    if not (isinstance(cfg.get("scan_url"), str) and cfg["scan_url"].startswith("https://")):
        errs.append("scan_url 需為 https:// 開頭的網址")
    if not isinstance(cfg.get("allowed_origins"), list):
        errs.append("allowed_origins 需為清單")
    if errs:
        raise ValueError("設定有誤，請修改 trader/order_config.yaml（不要改 order_defaults.yaml）：\n  - " + "\n  - ".join(errs))


def _read_yaml(p: Path) -> dict:
    try:
        d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"{p.name} 格式錯誤（冒號、縮排請用半形空白）：{e}") from None
    if not isinstance(d, dict):
        raise ValueError(f"{p.name} 格式錯誤：最外層必須是「項目: 值」")
    return d


def load_cfg(user_path: Path | None = None, defaults_path: Path | None = None) -> dict:
    base = _read_yaml(Path(defaults_path or CFG_DEFAULTS))
    p = Path(user_path or CFG_USER)
    over = _read_yaml(p) if p.exists() else {}

    def merge(a, b, pre=""):
        out = dict(a)
        for k, v in (b or {}).items():
            if k not in a:
                raise ValueError(f"order_config.yaml 有不認得的項目：{pre}{k}")
            out[k] = merge(a[k], v, f"{pre}{k}.") if isinstance(a[k], dict) else v
        return out
    cfg = merge(base, over)
    validate_cfg(cfg)
    return cfg


def origin_allowed(origin: str | None, allowed: list[str]) -> bool:
    if not origin:
        return False
    if origin in allowed or "*" in allowed:
        return origin != "null" or "null" in allowed
    u = urlparse(origin)
    if u.scheme in ("http", "https") and u.hostname in ("127.0.0.1", "localhost"):
        return True
    return False


def app_page(pair_code: str) -> bytes:
    """本機提供的完整畫面：把權杖寫進網頁，使用者不用輸入配對碼。
    其他網站讀不到這個回應（沒有 CORS 標頭，且禁止被嵌入 iframe）。"""
    html = APP_PAGE.read_text(encoding="utf-8")
    if TOKEN_META not in html:
        raise RuntimeError("index.html 缺少 trader-token 標籤")
    return html.replace(TOKEN_META, f'<meta name="trader-token" content="{pair_code}">', 1).encode("utf-8")


def make_handler(target, pair_code: str, allowed_origins: list[str], scan=None, on_quit=None):
    """target：App（主畫面登入流程）或已登入的 Engine（指令列模式、測試）。"""
    from .app_session import StaticApp
    app = target if hasattr(target, "session") else StaticApp(target)

    def applog(kind, msg):
        if app.engine is not None:
            app.engine.log(kind, msg)

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
                applog("auth", f"配對碼錯誤：{self.command} {self.path} 來源 {self.headers.get('Origin')}")
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

        def _engine(self):
            e = app.engine
            if e is None:
                self._json(409, {"ok": False, "error": "尚未登入凱基，請先登入", "need_login": True})
            return e

        # ── 路由 ──
        def do_OPTIONS(self):
            self.send_response(204 if (self._host_ok() and self._origin_ok()) else 403)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html", "/app"):
                if not self._host_ok():
                    return self._json(403, {"ok": False})
                body = app_page(pair_code)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/stage_scan.json":          # 選股資料是公開資料，不需權杖
                if not self._host_ok():
                    return self._json(403, {"ok": False})
                data = scan.read_bytes() if scan else None
                if data is None:
                    return self._json(404, {"ok": False, "error": "尚未下載到選股資料"})
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/status":
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
                if not self._guard(need_auth=False):
                    return
                engine = app.engine
                if engine is None:
                    return self._json(200, {"ok": True, "app": "stage-scan-trader", "mode": None, "state": app.state})
                self._json(200, {"ok": True, "app": "stage-scan-trader", "mode": engine.mode, "state": "ready",
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
            if path == "/api/session":
                return self._json(200, app.session())
            if path == "/api/scan_info":
                return self._json(200, scan.info() if scan else {"available": False})
            engine = self._engine()
            if engine is None:
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
            if path == "/api/login":
                ok, err = app.start_login(str(body.get("person_id", "")), str(body.get("password", "")),
                                          bool(body.get("remember")), str(body.get("mode", "")), str(body.get("confirm", "")))
                return self._json(200, {"ok": ok, "error": err})
            if path == "/api/select_account":
                ok, err = app.select_accounts(body.get("tw") or None, body.get("us") or None)
                return self._json(200, {"ok": ok, "error": err})
            if path == "/api/logout":
                ok, err = app.logout()
                return self._json(200, {"ok": ok, "error": err})
            if path == "/api/quit":                 # 主畫面的「結束程式」
                if on_quit is None:
                    return self._json(200, {"ok": False, "error": "指令列模式請在終端機按 Ctrl+C 結束"})
                self._json(200, {"ok": True})
                threading.Timer(0.3, on_quit).start()
                return
            if path == "/api/forget_password":
                app.forget_password()
                return self._json(200, {"ok": True})
            if path == "/api/scan_refresh":
                if not scan:
                    return self._json(200, {"ok": False, "error": "未啟用選股資料下載"})
                changed = scan.refresh()
                return self._json(200, {"ok": True, "changed": changed, **scan.info()})
            engine = self._engine()
            if engine is None:
                return
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


def _applog_factory(folder: Path):
    path = folder / "app.log"

    def log(kind: str, msg: str, **extra):
        from datetime import datetime
        from .market_hours import TPE
        rec = {"ts": datetime.now(TPE).isoformat(timespec="seconds"), "kind": kind, "msg": msg}
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass
    return log


def run_app_timer(app, stop: threading.Event, every: float = 1.0) -> None:
    while not stop.is_set():
        e = app.engine
        if e is not None:
            try:
                e.on_timer()
            except Exception as ex:  # noqa: BLE001
                e.log("error", f"計時器錯誤：{ex}")
        stop.wait(every)


class Services:
    """主畫面模式的下單程式：HTTP 伺服器、登入流程、計時器、選股資料下載。"""

    def __init__(self, port: int = PORT, cfg: dict | None = None, app=None, runtime: Path | None = None):
        from .app_session import App
        from .kgi_gateway import KgiBroker
        from .scan_feed import ScanFeed
        from .secret_store import default_store
        load_env()
        self.cfg = cfg or load_cfg()
        self.runtime = Path(runtime or RUNTIME)
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.log = _applog_factory(self.runtime)
        if app is None:
            app = App(self.cfg, self.runtime, default_store(),
                      broker_factory=lambda simulation, dry_run: KgiBroker(simulation=simulation, dry_run=dry_run,
                                                                            log=self.log),
                      log=self.log)
            env_pid = os.environ.get("KGI_PERSON_ID", "")
            if not app.profile.get("person_id") and env_pid:
                app.profile["person_id"] = env_pid.upper()   # 第一次使用時帶入 .env 的身分證字號
        self.app = app
        self.pair = os.environ.get("PAIR_CODE") or f"{secrets.randbelow(10**6):06d}"
        self.scan = ScanFeed(self.runtime, url=self.cfg["scan_url"], log=self.log)
        self.quit_hooks: list = []
        self.srv = ThreadingHTTPServer((HOST, port), make_handler(app, self.pair, self.cfg.get("allowed_origins", []),
                                                                  self.scan, on_quit=self.request_quit))
        self.port = self.srv.server_address[1]
        self.url = f"http://{HOST}:{self.port}/"
        self.stop = threading.Event()
        self._closed = False

    def start_background(self, serve: bool = True) -> None:
        threading.Thread(target=run_app_timer, args=(self.app, self.stop), daemon=True).start()
        threading.Thread(target=self.scan.run, args=(self.stop,), daemon=True).start()
        if serve:
            threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.app.auto_login()

    def request_quit(self) -> None:
        """「結束程式」或關閉視窗：先通知外層（例如關掉視窗），再停止服務。"""
        for f in list(self.quit_hooks):
            try:
                f()
            except Exception:  # noqa: BLE001
                pass
        self.shutdown()

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stop.set()
        app = self.app
        if app.engine is not None:
            app.engine.store.save()
            app.engine.store.log("stop", "下單程式結束")
        try:
            app.logout()
        except Exception:  # noqa: BLE001
            pass
        threading.Thread(target=self.srv.shutdown, daemon=True).start()


def existing_instance(port: int = PORT) -> bool:
    """這台電腦是否已經有下單程式在執行（避免開第二個）。"""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/ping", timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8")).get("app") == "stage-scan-trader"
    except (OSError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="階段掃描本機下單程式（凱基 SuperPy）")
    ap.add_argument("--live", action="store_true", help="正式環境：用 .env 的帳密直接登入（需輸入 YES）")
    ap.add_argument("--dry-run", action="store_true",
                    help="演練：用 .env 的帳密以正式環境登入，委託只在本機模擬成交，不送到凱基")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-browser", action="store_true", help="啟動後不要自動開啟畫面")
    a = ap.parse_args(argv)
    if a.live and a.dry_run:
        print("--live 與 --dry-run 只能擇一。")
        return 1

    load_env()
    try:
        cfg = load_cfg()
    except ValueError as e:
        print(f"[錯誤] {e}")
        return 3
    RUNTIME.mkdir(parents=True, exist_ok=True)
    applog = _applog_factory(RUNTIME)
    from .app_session import MODES, StaticApp
    from .kgi_gateway import BrokerError, KgiBroker
    from .scan_feed import ScanFeed

    env_pid, env_pwd = os.environ.get("KGI_PERSON_ID", ""), os.environ.get("KGI_PERSON_PWD", "")
    legacy = (a.live or a.dry_run) and env_pid and env_pwd
    if legacy:
        # 指令列模式：沿用 .env 帳密直接登入
        if a.live and not confirm_live():
            print("未輸入 YES，已取消。")
            return 1
        mode = "live" if a.live else "dry_run"
        store = Store(RUNTIME / mode)
        if a.dry_run:
            print("演練模式：以正式環境登入，只讀取行情、庫存與商品資料；委託一律在本機模擬成交，不會送到凱基。")
        broker = KgiBroker(simulation=False, log=store.log, dry_run=a.dry_run)
        try:
            broker.login(env_pid, env_pwd, os.environ.get("KGI_TW_ACCOUNT") or None,
                         os.environ.get("KGI_US_ACCOUNT") or None)
        except BrokerError as e:
            print(f"[錯誤] {e}")
            return 2
        engine = eng.Engine(broker, store, cfg, live=a.live)
        store.log("start", f"下單程式啟動：{MODES[mode]}")
        for n in engine.resume_after_restart():
            print("[接續]", n)
        app = StaticApp(engine)
        pair = os.environ.get("PAIR_CODE") or f"{secrets.randbelow(10**6):06d}"
        scan = ScanFeed(RUNTIME, url=cfg["scan_url"], log=applog)
        srv = ThreadingHTTPServer((HOST, a.port), make_handler(app, pair, cfg.get("allowed_origins", []), scan))
        stop = threading.Event()
        threading.Thread(target=run_app_timer, args=(app, stop), daemon=True).start()
        threading.Thread(target=scan.run, args=(stop,), daemon=True).start()
        print("=" * 60)
        print(f"  模式：{MODES[mode]}　台股 {broker.tw_account or '未設定'}　複委託 {broker.us_account or '未設定'}")
        print(f"  主畫面：http://{HOST}:{a.port}/")
        print(f"  配對碼：{pair}（只有從 GitHub 網頁連線時需要；主畫面會自動帶入）")
        print("  按 Ctrl+C 結束（狀態已存檔，下次啟動會接續監控）")
        print("=" * 60)
    else:
        if existing_instance(a.port):
            print(f"下單程式已經在執行中：http://{HOST}:{a.port}/")
            if not a.no_browser:
                import webbrowser
                webbrowser.open(f"http://{HOST}:{a.port}/")
            return 0
        if a.live or a.dry_run:
            print("trader/.env 沒有帳密，改用主畫面登入。")
        svc = Services(a.port, cfg)
        if a.live or a.dry_run:
            svc.app.profile["mode"] = "live" if a.live else "dry_run"
        svc.start_background(serve=False)
        app, srv, scan, stop, pair = svc.app, svc.srv, svc.scan, svc.stop, svc.pair
        print("=" * 60)
        print(f"  主畫面：{svc.url}（請在畫面上登入）")
        print(f"  配對碼：{pair}（只有從 GitHub 網頁連線時需要；主畫面會自動帶入）")
        print("  按 Ctrl+C 結束（狀態已存檔，下次啟動會接續監控）")
        print("=" * 60)
    if not a.no_browser:
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{HOST}:{a.port}/")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.server_close()
        if app.engine is not None:
            app.engine.store.save()
            app.engine.store.log("stop", "下單程式結束")
        if isinstance(app, StaticApp):
            app.engine.broker.logout()
        else:
            app.logout()
    return 0


if __name__ == "__main__":
    sys.exit(main())
