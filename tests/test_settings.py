"""交易參數讀寫與檢查的測試：python -m pytest tests 或 python tests/test_settings.py"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trader import settings as S  # noqa: E402


def _tmp():
    return Path(tempfile.mkdtemp()) / "config.yaml"


def test_defaults_are_valid():
    cfg = S.load(_tmp())
    assert cfg["daytrade"]["exit"]["take_profit_pct"] == 6.0
    assert cfg["daytrade"]["exit"]["force_close"] == "13:10"
    assert cfg["daytrade"]["entry"]["end"] == "10:00"
    assert cfg["swing"]["stop_loss_pct"] == 7.0


def test_set_save_reload_and_reset():
    p = _tmp()
    cfg = S.load(p)
    cfg = S.set_value(cfg, "daytrade.exit.take_profit_pct", S.parse_value("daytrade.exit.take_profit_pct", "5"))
    cfg = S.set_value(cfg, "daytrade.entry.end", S.parse_value("daytrade.entry.end", "10:30"))
    cfg = S.set_value(cfg, "universe.stages", S.parse_value("universe.stages", "2,4"))
    cfg = S.set_value(cfg, "daytrade.max_amount_per_trade", S.parse_value("daytrade.max_amount_per_trade", "500,000"))
    S.save(cfg, p)
    text = p.read_text(encoding="utf-8")
    assert "take_profit_pct: 5.0" in text and "swing" not in text  # 只存修改過的項目
    again = S.load(p)
    assert again["daytrade"]["exit"]["take_profit_pct"] == 5.0
    assert again["daytrade"]["entry"]["end"] == "10:30"           # 時間保持字串
    assert again["universe"]["stages"] == [2, 4]
    assert again["daytrade"]["max_amount_per_trade"] == 500000
    assert again["swing"]["stop_loss_pct"] == 7.0                 # 沒改的沿用預設
    back = S.reset(again, "daytrade.exit.take_profit_pct")
    assert back["daytrade"]["exit"]["take_profit_pct"] == 6.0
    S.save(S.reset(back), p)
    assert S.load(p) == S.defaults()


def test_nullable_take_profit():
    cfg = S.set_value(S.load(_tmp()), "daytrade.exit.take_profit_pct",
                      S.parse_value("daytrade.exit.take_profit_pct", "none"))
    assert cfg["daytrade"]["exit"]["take_profit_pct"] is None


def _rejected(key, text):
    cfg = S.load(_tmp())
    try:
        S.set_value(cfg, key, S.parse_value(key, text))
    except S.SettingsError:
        return True
    return False


def test_invalid_values_rejected():
    assert _rejected("swing.stop_loss_pct", "9")             # 書中上限 8%
    assert _rejected("daytrade.exit.force_close", "13:28")   # 收盤集合競價
    assert _rejected("daytrade.entry.end", "13:20")          # 晚於強制平倉
    assert _rejected("daytrade.entry.start", "09:05")        # 開盤區間還沒結束
    assert _rejected("daytrade.entry.start", "9點")
    assert _rejected("mode", "scalp")
    assert _rejected("universe.stages", "5")
    assert _rejected("daytrade.exit.stop_loss_pct", "abc")
    assert _rejected("swing.stop_loss_pct", "none")          # 停損不能不設


def test_bad_user_file_rejected():
    p = _tmp()
    p.write_text("daytrade:\n  exit:\n    stop_los_pct: 3\n", encoding="utf-8")
    try:
        S.load(p)
        assert False
    except S.SettingsError as e:
        assert "stop_los_pct" in str(e)


def test_failed_save_keeps_file():
    p = _tmp()
    S.save(S.set_value(S.load(p), "capital", 500000), p)
    before = p.read_text(encoding="utf-8")
    bad = S.load(p)
    bad["swing"]["stop_loss_pct"] = 20
    try:
        S.save(bad, p)
    except S.SettingsError:
        pass
    assert p.read_text(encoding="utf-8") == before


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
