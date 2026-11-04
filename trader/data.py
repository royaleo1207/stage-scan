"""行情資料：yfinance（需在可連 Yahoo Finance 的電腦執行）或 CSV。

CSV 格式：每檔一個檔案「代號.csv」，欄位 datetime（或 date）,open,high,low,close,volume，
volume 單位為「股」。日 K 放在 <資料夾>/daily/，分 K 放在 <資料夾>/<週期>/（例如 5m）。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

COLS = ["open", "high", "low", "close", "volume"]


def _norm(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):   # yfinance 單檔也會回傳（欄位, 代號）兩層
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns={c: str(c).lower() for c in df.columns})
    idx = pd.to_datetime(df.index)
    if idx.tz is not None:
        idx = idx.tz_convert("Asia/Taipei").tz_localize(None)
    df.index = idx
    df = df[COLS].astype(float).dropna(subset=["close"])
    return df[~df.index.duplicated()].sort_index()


def load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    tcol = next(c for c in df.columns if c.lower() in ("datetime", "date", "time", "ts"))
    return _norm(df.set_index(tcol))


def from_csv(folder: str, symbols: list[str], interval: str) -> dict[str, pd.DataFrame]:
    sub = Path(folder) / ("daily" if interval == "1d" else interval)
    out = {}
    for s in symbols:
        p = sub / f"{s}.csv"
        if p.exists():
            out[s] = load_csv(p)
    return out


def from_yfinance(symbols: list[str], interval: str) -> dict[str, pd.DataFrame]:
    """interval：1d（抓 5 年）、5m（最近 60 天）、1m（最近 7 天）。Yahoo 分鐘線期間有限。"""
    import yfinance as yf
    period = {"1d": "5y", "5m": "60d", "15m": "60d", "1m": "7d"}[interval]
    out = {}
    for s in symbols:
        for suffix in (".TW", ".TWO"):
            df = yf.download(s + suffix, period=period, interval=interval, progress=False,
                             auto_adjust=interval == "1d")
            if df is not None and len(df):
                out[s] = _norm(df)
                break
    return out


def load(source: str, symbols: list[str], interval: str, csv_dir: str = "data") -> dict[str, pd.DataFrame]:
    if source == "csv":
        return from_csv(csv_dir, symbols, interval)
    if source == "yfinance":
        return from_yfinance(symbols, interval)
    raise ValueError(f"不支援的資料來源：{source}")
