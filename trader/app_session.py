"""主畫面的登入流程：登入 → 選帳號 → 開始交易；登出；記住密碼與自動登入。

狀態（state）
- logged_out      尚未登入（顯示登入畫面）
- logging_in      登入中（凱基登入約需 20～40 秒）
- select_account  登入成功，請選證券／複委託帳號
- ready           可以交易

- 身分證字號、模式、上次選的帳號存在 runtime/profile.json；密碼只在勾選「記住密碼」時存進 Windows 認證管理員。
- 各模式的委託與部位分開存放（runtime/dry_run、runtime/simulation、runtime/live），
  避免正式模式去處理演練中的部位。
- 記住密碼時，演練與模擬模式啟動後自動登入；正式模式為了安全，仍需按「登入」並輸入「確認」。
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from . import engine as eng
from .secret_store import SecretStore
from .store import Store

MODES = {
    "dry_run": "演練（正式登入，本機模擬成交，不送單）",
    "simulation": "凱基模擬環境",
    "live": "正式下單（真實資金）",
}
LIVE_CONFIRM = "確認"


class _Tee(io.TextIOBase):
    """登入時同時把 SuperPy 訊息印到原本的地方並留一份，用來找出失敗原因。"""

    def __init__(self, orig):
        self.orig, self.buf = orig, io.StringIO()

    def write(self, s):
        self.buf.write(s)
        try:
            return self.orig.write(s) if self.orig else len(s)
        except (OSError, ValueError):
            return len(s)

    def flush(self):
        with contextlib.suppress(OSError, ValueError, AttributeError):
            self.orig.flush()


def _login_reason(text: str) -> str:
    m = re.findall(r"ReplyString:(.+)", text)
    if m:
        return m[-1].strip()
    for key in ("連線超時", "登入失敗", "CA", "Token無效"):
        for line in text.splitlines()[::-1]:
            if key in line:
                return line.strip()
    return ""


def mask_id(pid: str) -> str:
    return pid[:3] + "*" * max(0, len(pid) - 6) + pid[-3:] if len(pid) > 6 else pid


class App:
    def __init__(self, cfg: dict, runtime: Path, secrets: SecretStore,
                 broker_factory: Callable[..., Any], clock=None, log: Callable[..., Any] | None = None):
        self.cfg, self.runtime, self.secrets = cfg, Path(runtime), secrets
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.broker_factory = broker_factory
        self.clock = clock
        self._log = log or (lambda *a, **k: None)
        self.lock = threading.RLock()
        self.state, self.message, self.error = "logged_out", "", ""
        self.broker = None
        self.engine: eng.Engine | None = None
        self.mode = None
        self.accounts: list[dict] = []
        self.profile_path = self.runtime / "profile.json"
        self.profile = {"person_id": "", "remember": False, "mode": "dry_run", "tw_account": "", "us_account": ""}
        with contextlib.suppress(OSError, ValueError):
            self.profile.update(json.loads(self.profile_path.read_text(encoding="utf-8")))

    # ── 狀態 ──
    def save_profile(self) -> None:
        with contextlib.suppress(OSError):
            self.profile_path.write_text(json.dumps(self.profile, ensure_ascii=False, indent=1), encoding="utf-8")

    def has_saved_password(self) -> bool:
        pid = self.profile.get("person_id")
        if not (pid and self.profile.get("remember") and self.secrets.available):
            return False
        try:
            return bool(self.secrets.load(pid))
        except OSError:
            return False

    def session(self) -> dict:
        with self.lock:
            return {
                "state": self.state, "message": self.message, "error": self.error, "mode": self.mode,
                "modes": MODES, "live_confirm": LIVE_CONFIRM,
                "profile": dict(self.profile, has_saved_password=self.has_saved_password()),
                "remember_available": self.secrets.available,
                "accounts": [{"account": str(r.get("account")), "flag": str(r.get("account_flag", ""))}
                             for r in self.accounts if str(r.get("account_flag", "")) in ("證券", "複委託")],
                "user": mask_id(self.profile.get("person_id", "")) if self.state == "ready" else "",
                "tw_account": getattr(self.broker, "tw_account", None) if self.state == "ready" else None,
                "us_account": getattr(self.broker, "us_account", None) if self.state == "ready" else None,
            }

    # ── 登入 ──
    def start_login(self, person_id: str, password: str, remember: bool, mode: str, confirm: str = "") -> tuple[bool, str]:
        with self.lock:
            if self.state in ("logging_in",):
                return False, "正在登入中，請稍候"
            if self.state in ("ready", "select_account"):
                return False, "已經登入，請先登出"
            pid = (person_id or "").strip().upper()
            if not re.fullmatch(r"[A-Z][0-9A-Z][0-9]{8}", pid):
                return False, "身分證字號格式不正確（例如 A123456789）"
            if mode not in MODES:
                return False, "請選擇模式"
            if mode == "live" and (confirm or "").strip() != LIVE_CONFIRM:
                return False, f"正式下單需要輸入「{LIVE_CONFIRM}」"
            pwd = password or ""
            if not pwd and pid == self.profile.get("person_id") and self.has_saved_password():
                pwd = self.secrets.load(pid) or ""
            if not pwd:
                return False, "請輸入密碼"
            self.state, self.error, self.message = "logging_in", "", "登入凱基中，約需 20～40 秒…"
            self.mode = mode
            threading.Thread(target=self._do_login, args=(pid, pwd, bool(remember), mode), daemon=True).start()
            return True, ""

    def _do_login(self, pid: str, pwd: str, remember: bool, mode: str) -> None:
        tee = _Tee(sys.stdout)
        try:
            broker = self.broker_factory(simulation=(mode == "simulation"), dry_run=(mode == "dry_run"))
            with contextlib.redirect_stdout(tee):
                broker.connect(pid, pwd)
        except Exception as e:  # noqa: BLE001  任何登入錯誤都要回到登入畫面
            why = _login_reason(tee.buf.getvalue())
            with self.lock:
                self.state, self.message = "logged_out", ""
                self.error = f"{e}" + (f"｜凱基回覆：{why}" if why and why not in str(e) else "")
            self._log("login", f"登入失敗：{self.error}")
            return
        with self.lock:
            self.broker = broker
            self.accounts = broker.accounts
            same_user = self.profile.get("person_id") == pid
            self.profile.update(person_id=pid, remember=remember and self.secrets.available, mode=mode)
            if not same_user:
                self.profile.update(tw_account="", us_account="")
            try:
                if self.profile["remember"]:
                    self.secrets.save(pid, pwd)
                else:
                    self.secrets.delete(pid)
            except OSError as e:
                self.profile["remember"] = False
                self._log("error", f"無法記住密碼：{e}")
            self.save_profile()
            self._log("login", f"{mask_id(pid)} 登入成功（{MODES[mode]}）")
            tw, us = self.profile.get("tw_account"), self.profile.get("us_account")
            valid = {str(r.get("account")) for r in self.accounts}
            if (tw or us) and (not tw or tw in valid) and (not us or us in valid):
                ok, err = self._select(tw, us)
                if ok:
                    return
            self.state, self.message = "select_account", "請選擇要使用的帳號"

    def select_accounts(self, tw: str | None, us: str | None) -> tuple[bool, str]:
        with self.lock:
            if self.state != "select_account":
                return False, "請先登入"
            return self._select(tw or None, us or None)

    def _select(self, tw: str | None, us: str | None) -> tuple[bool, str]:
        try:
            self.broker.select(tw, us)
        except Exception as e:  # noqa: BLE001
            self.state, self.error = "select_account", str(e)
            return False, str(e)
        store = Store(self.runtime / self.mode)
        self.engine = eng.Engine(self.broker, store, self.cfg, live=(self.mode == "live"), clock=self.clock)
        store.log("start", f"開始交易：{MODES[self.mode]}，台股 {tw or '未使用'}、複委託 {us or '未使用'}")
        for n in self.engine.resume_after_restart():
            store.log("restart", n)
        self.profile.update(tw_account=tw or "", us_account=us or "")
        self.save_profile()
        self.state, self.message, self.error = "ready", "", ""
        return True, ""

    def auto_login(self) -> bool:
        """記住密碼時自動登入（正式模式除外）。"""
        p = self.profile
        if p.get("mode") == "live" or not self.has_saved_password():
            return False
        ok, _ = self.start_login(p["person_id"], "", True, p.get("mode", "dry_run"))
        return ok

    def logout(self) -> tuple[bool, str]:
        with self.lock:
            if self.engine:
                self.engine.store.log("stop", "使用者登出（部位與出場規則已存檔，下次登入同一模式會接續監控）")
                self.engine.store.save()
            self.engine = None
            if self.broker is not None:
                with contextlib.suppress(Exception):
                    self.broker.logout()
            self.broker, self.accounts, self.mode = None, [], None
            self.state, self.message, self.error = "logged_out", "", ""
            return True, ""

    def forget_password(self) -> None:
        with self.lock:
            pid = self.profile.get("person_id")
            if pid:
                with contextlib.suppress(OSError):
                    self.secrets.delete(pid)
            self.profile["remember"] = False
            self.save_profile()


class StaticApp:
    """已經登入好的引擎（指令列模式與測試用），介面同 App。"""

    def __init__(self, engine: eng.Engine):
        self.engine = engine
        self.state = "ready"

    def session(self) -> dict:
        b = self.engine.broker
        return {"state": "ready", "mode": self.engine.mode, "message": "", "error": "", "modes": MODES,
                "profile": {}, "accounts": [], "remember_available": False, "user": "",
                "tw_account": getattr(b, "tw_account", None), "us_account": getattr(b, "us_account", None),
                "static": True}

    def start_login(self, *a, **k):
        return False, "已用指令列登入"

    def select_accounts(self, *a, **k):
        return False, "已用指令列登入"

    def logout(self):
        return False, "指令列模式請在終端機按 Ctrl+C 結束"

    def forget_password(self):
        pass
