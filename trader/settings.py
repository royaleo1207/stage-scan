"""交易參數：預設值 + 使用者設定。

- trader/defaults.yaml：預設值（隨程式更新，不要手改）
- trader/config.yaml：使用者設定，只記錄跟預設值不同的項目（不提交到儲存庫）

程式使用 load() 取得合併後的設定；修改用 set_value()／reset() 後 save()。
每次儲存前都會整份檢查，不合法的值不會寫入。

指令列用法：
    python -m trader.settings show [項目]
    python -m trader.settings diff
    python -m trader.settings set 項目 值
    python -m trader.settings reset [項目]
"""

from __future__ import annotations

import copy
import os
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
DEFAULTS_PATH = HERE / "defaults.yaml"
USER_PATH = HERE / "config.yaml"

# 可以留空（null）表示「不設」的項目
NULLABLE = {"daytrade.exit.take_profit_pct", "swing.partial_take_pct"}

CHOICES = {
    "mode": {"daytrade", "swing"},
    "broker": {"paper", "kgi_sim", "kgi_live"},
    "order_confirm": {"manual", "auto"},
    "daytrade.direction": {"long", "short"},
    "swing.entry_timing": {"intraday", "next_open"},
    "notify.channel": {"line", "none"},
}

# 數值範圍（含端點）：(最小, 最大)
RANGES = {
    "max_watchlist": (1, 900),
    "confirm_timeout_sec": (5, 600),
    "capital": (1, None),
    "universe.min_rs": (0, 99),
    "universe.min_avg_volume_lots": (0, None),
    "daytrade.opening_range_minutes": (1, 60),
    "daytrade.min_range_pct": (0, 10),
    "daytrade.max_range_pct": (0, 10),
    "daytrade.or_volume_ratio": (0, 10),
    "daytrade.limit_buffer_pct": (0, 10),
    "daytrade.exit.stop_loss_pct": (0.1, 10),
    "daytrade.exit.take_profit_pct": (0.1, 10),
    "daytrade.max_amount_per_trade": (1, None),
    "daytrade.max_entries_per_symbol": (1, 10),
    "swing.max_contraction": (0, 5),
    "swing.max_vol_ratio": (0, 5),
    "swing.pivot_lookback": (5, 250),
    "swing.breakout_volume_ratio": (0, 10),
    "swing.max_chase_pct": (0, 20),
    "swing.stop_loss_pct": (0.1, 8),        # 書中停損上限 8%
    "swing.breakeven_r": (0, 10),
    "swing.ma_break_volume_ratio": (0, 10),
    "swing.partial_take_pct": (0.1, 200),
    "swing.partial_take_size": (0.01, 1),
    "swing.max_position_pct": (0.1, 100),
    "risk.risk_per_trade_pct": (0.01, 10),
    "risk.max_positions": (1, 50),
    "risk.max_daily_loss_pct": (0.1, 50),
    "risk.max_orders_per_day": (1, 1000),
    "costs.fee_rate": (0, 0.01),
    "costs.fee_discount": (0, 1),
    "costs.min_fee": (0, 1000),
    "costs.tax_rate": (0, 0.01),
    "costs.tax_day_trade": (0, 0.01),
    "costs.slippage_ticks": (0, 20),
}

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MARKET_OPEN, MARKET_CLOSE = "09:00", "13:30"


class SettingsError(ValueError):
    pass


# ── 讀寫 ──────────────────────────────────────────────

def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise SettingsError(f"{path.name} 格式錯誤：最外層必須是「項目: 值」")
    return data


def defaults() -> dict:
    return _read_yaml(DEFAULTS_PATH)


def _merge(base: dict, over: dict, prefix: str = "") -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        key = f"{prefix}{k}"
        if k not in base:
            raise SettingsError(f"不認得的項目：{key}")
        if isinstance(base[k], dict):
            if not isinstance(v, dict):
                raise SettingsError(f"{key} 應該是一組設定，不是單一值")
            out[k] = _merge(base[k], v, key + ".")
        else:
            out[k] = v
    return out


def load(user_path: Path | None = None) -> dict:
    """回傳合併後、已檢查過的設定。"""
    cfg = _merge(defaults(), _read_yaml(user_path or USER_PATH))
    validate(cfg)
    return cfg


def _diff(base: dict, cur: dict) -> dict:
    out = {}
    for k, v in cur.items():
        if isinstance(v, dict):
            d = _diff(base[k], v)
            if d:
                out[k] = d
        elif v != base[k]:
            out[k] = v
    return out


def save(cfg: dict, user_path: Path | None = None) -> Path:
    """檢查後只把跟預設值不同的項目寫入 config.yaml（保留上一版為 .bak）。"""
    validate(cfg)
    path = user_path or USER_PATH
    over = _diff(defaults(), cfg)
    text = ("# 個人交易設定：只記錄跟 trader/defaults.yaml 不同的項目。\n"
            "# 建議用 python -m trader.settings set／reset 修改，程式會先檢查再儲存。\n")
    if over:
        text += yaml.safe_dump(over, allow_unicode=True, sort_keys=False)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8")
    if path.exists():
        os.replace(path, path.with_suffix(".yaml.bak"))
    os.replace(tmp, path)
    return path


# ── 取值與修改 ────────────────────────────────────────

def _walk(cfg: dict, key: str):
    parts = key.split(".")
    node = cfg
    for p in parts[:-1]:
        if not isinstance(node.get(p), dict):
            raise SettingsError(f"不認得的項目：{key}")
        node = node[p]
    if parts[-1] not in node:
        raise SettingsError(f"不認得的項目：{key}")
    return node, parts[-1]


def get(cfg: dict, key: str):
    node, leaf = _walk(cfg, key)
    return node[leaf]


def parse_value(key: str, text: str):
    """把使用者輸入的文字轉成跟預設值相同的型別。"""
    node, leaf = _walk(defaults(), key)
    ref = node[leaf]
    t = text.strip()
    if isinstance(ref, dict):
        raise SettingsError(f"{key} 是一組設定，請指定底下的項目")
    if t.lower() in ("null", "none", "") and key in NULLABLE:
        return None
    try:
        if isinstance(ref, bool):
            low = t.lower()
            if low in ("true", "yes", "on", "1", "是", "開"):
                return True
            if low in ("false", "no", "off", "0", "否", "關"):
                return False
            raise ValueError
        if isinstance(ref, int):
            v = float(t.replace(",", ""))
            if v != int(v):
                raise ValueError
            return int(v)
        if isinstance(ref, float):
            return float(t.replace(",", ""))
        if isinstance(ref, list):
            items = [x for x in re.split(r"[,\s\[\]]+", t) if x]
            return [int(x) for x in items]
        return t  # 字串（含時間 "10:00"，不交給 YAML 解析，避免被當成數字）
    except ValueError:
        kind = {bool: "true 或 false", int: "整數", float: "數字", list: "以逗號分隔的整數"}
        raise SettingsError(f"{key} 需要{kind.get(type(ref), '文字')}，收到「{text}」") from None


def set_value(cfg: dict, key: str, value) -> dict:
    """回傳修改後的新設定（已檢查）；原本的 cfg 不變。"""
    new = copy.deepcopy(cfg)
    node, leaf = _walk(new, key)
    node[leaf] = value
    validate(new)
    return new


def reset(cfg: dict, key: str | None = None) -> dict:
    if key is None:
        return defaults()
    new = copy.deepcopy(cfg)
    node, leaf = _walk(new, key)
    dnode, _ = _walk(defaults(), key)
    node[leaf] = copy.deepcopy(dnode[leaf])
    validate(new)
    return new


# ── 檢查 ──────────────────────────────────────────────

def _flatten(d: dict, prefix: str = ""):
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            yield from _flatten(v, key + ".")
        else:
            yield key, v


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def validate(cfg: dict) -> None:
    errs = []
    ref = dict(_flatten(defaults()))
    cur = dict(_flatten(cfg))

    for key, v in cur.items():
        if key not in ref:
            errs.append(f"不認得的項目：{key}")
            continue
        r = ref[key]
        if v is None:
            if key not in NULLABLE:
                errs.append(f"{key} 不能留空")
            continue
        if isinstance(r, bool):
            ok = isinstance(v, bool)
        elif isinstance(r, (int, float)):
            ok = isinstance(v, (int, float)) and not isinstance(v, bool)
        else:
            ok = isinstance(v, type(r))
        if not ok:
            errs.append(f"{key} 型別錯誤：{v!r}")
            continue
        if key in CHOICES and v not in CHOICES[key]:
            errs.append(f"{key} 只能是 {'、'.join(sorted(CHOICES[key]))}，收到「{v}」")
        if key in RANGES:
            lo, hi = RANGES[key]
            if (lo is not None and v < lo) or (hi is not None and v > hi):
                rng = f"{lo} ～ {hi}" if hi is not None else f"至少 {lo}"
                errs.append(f"{key} 超出範圍（{rng}），收到 {v}")

    stages = cur.get("universe.stages")
    if isinstance(stages, list):
        if not stages or any(s not in (1, 2, 3, 4) for s in stages):
            errs.append("universe.stages 需為 1～4 的階段，至少一個")

    times = {k: cur.get(k) for k in ("daytrade.entry.start", "daytrade.entry.end", "daytrade.exit.force_close")}
    bad = [k for k, v in times.items() if not (isinstance(v, str) and TIME_RE.match(v))]
    for k in bad:
        errs.append(f"{k} 需為 HH:MM 格式（例如 09:15），收到「{times[k]}」")
    if not bad:
        s, e, f = (_minutes(times[k]) for k in times)
        or_end = _minutes(MARKET_OPEN) + int(cur.get("daytrade.opening_range_minutes") or 0)
        if s < or_end:
            errs.append(f"daytrade.entry.start 不能早於開盤區間結束（{or_end // 60:02d}:{or_end % 60:02d}）")
        if not s < e:
            errs.append("daytrade.entry.end 必須晚於 daytrade.entry.start")
        if not e < f:
            errs.append("daytrade.exit.force_close 必須晚於 daytrade.entry.end")
        if f > _minutes("13:25"):
            errs.append("daytrade.exit.force_close 最晚 13:25（13:25 後進入收盤集合競價）")

    lo, hi = cur.get("daytrade.min_range_pct"), cur.get("daytrade.max_range_pct")
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and lo >= hi:
        errs.append("daytrade.min_range_pct 必須小於 daytrade.max_range_pct")

    if errs:
        raise SettingsError("設定有誤：\n  - " + "\n  - ".join(errs))


def warnings(cfg: dict) -> list[str]:
    """合法但需要提醒的組合。"""
    w = []
    if cfg["broker"] == "kgi_live" and cfg["order_confirm"] == "auto":
        w.append("目前為「正式環境 + 全自動免確認」：程式會直接用真實資金下單。")
    if cfg["mode"] == "swing" and 2 not in cfg["universe"]["stages"]:
        w.append("波段模式依書中規則只做第二階段，目前選的階段不含 2。")
    if cfg["mode"] == "daytrade" and cfg["daytrade"]["direction"] == "short" and 2 in cfg["universe"]["stages"]:
        w.append("當沖做空卻選了第二階段（上升趨勢），方向與趨勢相反。")
    return w


# ── 指令列 ────────────────────────────────────────────

def _show(cfg: dict, key: str | None = None) -> str:
    over = dict(_flatten(_diff(defaults(), cfg)))
    rows = [(k, v) for k, v in _flatten(cfg) if key is None or k == key or k.startswith(key + ".")]
    if not rows:
        raise SettingsError(f"不認得的項目：{key}")
    width = max(len(k) for k, _ in rows)
    return "\n".join(f"{k.ljust(width)}  {'—' if v is None else v}{'   ← 已修改' if k in over else ''}"
                     for k, v in rows)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "show"
    try:
        cfg = load()
        if cmd == "show":
            print(_show(cfg, argv[1] if len(argv) > 1 else None))
        elif cmd == "diff":
            over = _diff(defaults(), cfg)
            print(yaml.safe_dump(over, allow_unicode=True, sort_keys=False).strip() if over else "全部使用預設值")
        elif cmd == "set" and len(argv) == 3:
            key = argv[1]
            old = get(cfg, key)
            cfg = set_value(cfg, key, parse_value(key, argv[2]))
            save(cfg)
            print(f"已儲存：{key}  {old} → {get(cfg, key)}")
        elif cmd == "reset" and len(argv) <= 2:
            key = argv[1] if len(argv) == 2 else None
            cfg = reset(cfg, key)
            save(cfg)
            print(f"已改回預設：{key or '全部項目'}")
        else:
            print(__doc__.strip())
            return 2
        for w in warnings(cfg):
            print(f"⚠ {w}")
        return 0
    except SettingsError as e:
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
