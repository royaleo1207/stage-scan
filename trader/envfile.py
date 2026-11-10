"""讀取 trader/.env（帳密只放這裡，.env 已加入 .gitignore，不會提交）。"""

from __future__ import annotations

import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent / ".env"


def load_env(path: Path | None = None, override: bool = False) -> dict[str, str]:
    p = Path(path or ENV_PATH)
    out: dict[str, str] = {}
    if not p.exists():
        return out
    for raw in p.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k] = v
        if override or k not in os.environ:
            os.environ[k] = v
    return out
