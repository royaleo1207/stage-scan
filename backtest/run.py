"""回測指令。

    python -m backtest.run daytrade --symbols 2330 2454 3008
    python -m backtest.run daytrade --scan stage_scan.json --interval 5m
    python -m backtest.run swing --symbols 2330 2454 ... --start 2023-01-01
    python -m backtest.run swing --source csv --csv-dir data --symbols 2330 2454

參數一律讀 trader 的設定（預設值 + 你的 config.yaml），用 --set 可臨時覆寫、不會儲存：
    python -m backtest.run daytrade --symbols 2330 --set daytrade.exit.take_profit_pct=4

結果存在 backtest/results/<模式>_<時間>/：trades.csv、summary.txt
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trader import daytrade, settings, swing  # noqa: E402
from trader import data as D  # noqa: E402
from trader.universe import load_scan  # noqa: E402


def summarize(pnl: pd.Series, capital: float, r: pd.Series | None = None, equity: pd.Series | None = None) -> dict:
    n = len(pnl)
    if n == 0:
        return {"交易次數": 0}
    win, loss = pnl[pnl > 0], pnl[pnl <= 0]
    curve = equity if equity is not None and len(equity) else capital + pnl.cumsum()
    dd = (curve / curve.cummax() - 1).min()
    out = {
        "交易次數": n,
        "勝率": f"{len(win) / n:.1%}",
        "平均獲利（元）": round(win.mean(), 0) if len(win) else 0,
        "平均虧損（元）": round(loss.mean(), 0) if len(loss) else 0,
        "賺賠比": round(win.mean() / -loss.mean(), 2) if len(win) and len(loss) and loss.mean() < 0 else None,
        "獲利因子": round(win.sum() / -loss.sum(), 2) if len(loss) and loss.sum() < 0 else None,
        "總損益（元）": round(pnl.sum(), 0),
        "報酬率（對總資金）": f"{pnl.sum() / capital:.2%}",
        "最大回撤": f"{dd:.2%}",
    }
    if r is not None and r.notna().any():
        out["平均 R"] = round(r.mean(), 2)
    return out


def _filters_from_daily(daily: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """當沖回測的選股條件：用「前一天」的階段與趨勢樣板（RS 需整個市場排名，回測略過）。"""
    ind = swing.indicators(daily)
    ok = ind["stage"].isin(cfg["universe"]["stages"])
    if cfg["universe"]["trend_template"]:
        ok &= ind["tt7"]
    ok &= ind["avg_vol50"] >= cfg["universe"]["min_avg_volume_lots"] * 1000
    return pd.DataFrame({"ok": ok.shift(1, fill_value=False), "prev_close": ind["close"].shift(1),
                         "avg_vol50": ind["avg_vol50"].shift(1)})


def run_daytrade(symbols, cfg, a):
    intraday = D.load(a.source, symbols, a.interval, a.csv_dir)
    daily = D.load(a.source, symbols, "1d", a.csv_dir)
    notes = []
    if not intraday:
        sys.exit("沒有取得任何分 K 資料。")
    filt = {}
    for s in intraday:
        if s in daily and len(daily[s]) >= 260:
            filt[s] = _filters_from_daily(daily[s], cfg)
        else:
            notes.append(f"{s}：日 K 不足，無法判斷階段，回測時不套用選股條件")
    days = sorted(set().union(*[set(df.index.normalize()) for df in intraday.values()]))
    trades, skipped = [], {}
    for d in days:
        bars, info = {}, {}
        for s, df in intraday.items():
            day = df[df.index.normalize() == d]
            if day.empty:
                continue
            if s in filt:
                f = filt[s].reindex([d])
                if pd.isna(f["prev_close"].iloc[0]):
                    skipped["日 K 未涵蓋當天"] = skipped.get("日 K 未涵蓋當天", 0) + 1
                    continue
                if not bool(f["ok"].iloc[0]):
                    skipped["不符選股條件（階段／趨勢樣板／均量）"] = skipped.get("不符選股條件（階段／趨勢樣板／均量）", 0) + 1
                    continue
                pc, av = float(f["prev_close"].iloc[0]), float(f["avg_vol50"].iloc[0])
            else:
                prior = df[df.index < d]
                if prior.empty:
                    continue
                pc, av = float(prior["close"].iloc[-1]), None
            bars[s], info[s] = day, {"prev_close": pc, "avg_vol50": av}
        if not bars:
            continue
        t, st = daytrade.run_day(bars, info, cfg)
        trades += t
        for k, v in st["skipped"].items():
            skipped[k] = skipped.get(k, 0) + v
    cols = list(daytrade.Trade.__dataclass_fields__) + ["r"]
    df = pd.DataFrame([{**t.__dict__, "r": round(t.r, 2)} for t in trades], columns=cols)
    summ = summarize(df["pnl"] if len(df) else pd.Series(dtype=float), cfg["capital"], df["r"] if len(df) else None)
    period = f"{days[0]:%Y-%m-%d} ～ {days[-1]:%Y-%m-%d}（{len(days)} 個交易日，{a.interval} K）"
    return df, summ, skipped, notes, period


def run_swing(symbols, cfg, a):
    daily = D.load(a.source, symbols, "1d", a.csv_dir)
    daily = {s: df for s, df in daily.items() if len(df) >= 260}
    if not daily:
        sys.exit("沒有取得足夠的日 K 資料（每檔至少約 260 個交易日）。")
    res = swing.run(daily, cfg, start=a.start)
    df, eq = res["trades"], res["equity"]
    notes = [] if res["rs_used"] else ["股票少於 10 檔，無法排名 RS，回測略過 RS 條件"]
    if res["open"]:
        notes.append(f"期末仍持有 {len(res['open'])} 檔（未計入已平倉交易）："
                     + "、".join(p["symbol"] for p in res["open"]))
    if len(eq):
        notes.append(f"期末資產 {eq.iloc[-1]:,.0f} 元（含未平倉市值）")
    summ = summarize(df["pnl"] if len(df) else pd.Series(dtype=float), cfg["capital"],
                     df["r"] if len(df) else None, eq)
    period = f"{eq.index[0]:%Y-%m-%d} ～ {eq.index[-1]:%Y-%m-%d}" if len(eq) else ""
    return df, summ, res["skipped"], notes, period


def main(argv=None):
    ap = argparse.ArgumentParser(description="台股自動交易回測")
    ap.add_argument("mode", choices=["daytrade", "swing"])
    ap.add_argument("--symbols", nargs="*", default=[], help="股票代號，例如 2330 2454")
    ap.add_argument("--scan", help="改用 stage_scan.json 的台股名單（依目前選股條件）")
    ap.add_argument("--source", default="yfinance", choices=["yfinance", "csv"])
    ap.add_argument("--csv-dir", default="data")
    ap.add_argument("--interval", default="5m", choices=["1m", "5m", "15m"], help="當沖分 K 週期")
    ap.add_argument("--start", help="波段回測起始日 YYYY-MM-DD")
    ap.add_argument("--set", action="append", default=[], metavar="項目=值", help="臨時覆寫設定（不儲存）")
    ap.add_argument("--out", default=str(ROOT / "backtest" / "results"))
    a = ap.parse_args(argv)

    cfg = settings.load()
    for kv in a.set:
        k, _, v = kv.partition("=")
        cfg = settings.set_value(cfg, k, settings.parse_value(k, v))
    symbols = list(a.symbols)
    if a.scan:
        scan = load_scan(a.scan)
        u = cfg["universe"]
        for s in scan.get("stocks", []):
            if s.get("market") == "TW" and s.get("stage") in u["stages"] and (s.get("rs") or 0) >= u["min_rs"]:
                symbols.append(s["ticker"].split(".")[0])
        symbols = symbols[: cfg["max_watchlist"]] if a.mode == "daytrade" else symbols
    if not symbols:
        sys.exit("請用 --symbols 或 --scan 指定股票。")

    df, summ, skipped, notes, period = (run_daytrade if a.mode == "daytrade" else run_swing)(symbols, cfg, a)

    out = Path(a.out) / f"{a.mode}_{datetime.now():%Y%m%d_%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    df.to_csv(out / "trades.csv", index=False, encoding="utf-8-sig")
    lines = [f"回測：{'當沖（開盤區間突破）' if a.mode == 'daytrade' else '波段（超級績效）'}",
             f"期間：{period}", f"股票：{len(symbols)} 檔", ""]
    lines += [f"{k}：{v}" for k, v in summ.items()]
    if skipped:
        lines += ["", "未進場原因（次數）："] + [f"  {k}：{v}" for k, v in sorted(skipped.items(), key=lambda x: -x[1])]
    if notes:
        lines += ["", "注意："] + [f"  - {n}" for n in notes]
    if a.set:
        lines += ["", "本次臨時覆寫：" + "、".join(a.set)]
    lines += ["", "回測結果不代表未來績效；成交假設見 trader/daytrade.py、trader/swing.py 開頭說明。"]
    text = "\n".join(lines)
    (out / "summary.txt").write_text(text, encoding="utf-8")
    print(text)
    print(f"\n明細：{out / 'trades.csv'}")


if __name__ == "__main__":
    main()
