#!/usr/bin/env python3
"""
階段掃描器 — 依《超級績效》（Mark Minervini）的四階段與趨勢樣板，
掃描美股、台股與全球 ETF，輸出每檔所在階段，並標出「正在起漲」的標的。

安裝：
    pip install yfinance pandas numpy lxml requests

用法：
    python stage_scanner.py                      # 美股 S&P 500 + Nasdaq-100、台股上市櫃、全球 ETF
    python stage_scanner.py --markets tw etf     # 只掃台股與 ETF
    python stage_scanner.py --nasdaq-all         # 改掃全部 Nasdaq 掛牌普通股（約 3000+ 檔，較慢）
    python stage_scanner.py --limit 50           # 每個市場只取前 50 檔，快速試跑

輸出：
    stage_scan.json  → 拖進網頁儀表板查看
    stage_scan.csv   → 用 Excel 開啟

本程式只做技術面篩選，不構成投資建議。
"""

import argparse
import io
import json
import math
import sys
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

UA = {"User-Agent": "Mozilla/5.0 (stage-scanner)"}

# ──────────────────────────────────────────────────────────────
# 參數（依書中原則；可自行調整）
# ──────────────────────────────────────────────────────────────
SLOPE_WINDOW = 21          # 200 日均線斜率：比較 21 個交易日（約一個月）
SLOPE_BAND = 0.0075        # 一個月變動在 ±0.75% 內視為「走平」
PRIOR_SHIFT = 105          # 走平時回看約 5～6 個月前的均線方向，判斷是頭部還是底部
EMERGING_MAX_DAYS = 40     # 轉入第二階段 40 個交易日內，算「正在起漲」
EMERGING_NEAR_HIGH = 0.85  # 且股價在 52 週高點的 85% 以上
CHART_WEEKS = 78           # 網頁走勢圖顯示的週數

GLOBAL_ETFS = {
    # 美國大盤與風格
    "SPY": "S&P 500", "QQQ": "Nasdaq-100", "DIA": "道瓊工業", "IWM": "羅素 2000",
    "MDY": "S&P 中型股", "RSP": "S&P 500 等權重", "MTUM": "美股動能因子",
    # 美股類股
    "XLK": "科技", "XLF": "金融", "XLE": "能源", "XLV": "醫療保健", "XLI": "工業",
    "XLY": "非必需消費", "XLP": "必需消費", "XLU": "公用事業", "XLB": "原物料",
    "XLRE": "不動產", "XLC": "通訊服務", "SMH": "半導體", "IBB": "生技",
    "ITA": "航太國防", "KRE": "區域銀行", "XHB": "房屋建築", "IGV": "軟體",
    "TAN": "太陽能", "URA": "鈾礦", "COPX": "銅礦", "GDX": "金礦股",
    # 國際股市
    "EFA": "已開發市場", "EEM": "新興市場", "VGK": "歐洲", "EWJ": "日本",
    "EWT": "台灣", "EWY": "南韓", "FXI": "中國大型股", "KWEB": "中國網路",
    "INDA": "印度", "EWZ": "巴西", "EWG": "德國", "EWU": "英國", "EWC": "加拿大",
    "EWA": "澳洲", "EWW": "墨西哥", "VNM": "越南", "EWS": "新加坡", "EIDO": "印尼",
    "THD": "泰國", "EWH": "香港", "ARGT": "阿根廷", "TUR": "土耳其",
    # 債券、商品、貨幣
    "TLT": "美國長天期公債", "IEF": "美國中期公債", "SHY": "美國短期公債",
    "HYG": "高收益債", "LQD": "投資級公司債", "EMB": "新興市場債",
    "GLD": "黃金", "SLV": "白銀", "USO": "原油", "UNG": "天然氣",
    "DBA": "農產品", "DBC": "綜合商品", "UUP": "美元指數",
    "FXY": "日圓", "FXE": "歐元",
    # 台灣 ETF
    "0050.TW": "元大台灣50", "0056.TW": "元大高股息", "006208.TW": "富邦台50",
    "00878.TW": "國泰永續高股息", "00881.TW": "國泰台灣5G+",
    "00891.TW": "中信關鍵半導體", "00892.TW": "富邦台灣半導體",
}


# ──────────────────────────────────────────────────────────────
# 股票清單
# ──────────────────────────────────────────────────────────────
def _get(url, encoding=None):
    import requests
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    if encoding:
        r.encoding = encoding
    return r.text


def universe_sp500():
    html = _get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
    df = pd.read_html(io.StringIO(html), attrs={"id": "constituents"})[0]
    return [(str(s).replace(".", "-"), str(n), "US") for s, n in zip(df["Symbol"], df["Security"])]


def universe_nasdaq100():
    html = _get("https://en.wikipedia.org/wiki/Nasdaq-100")
    for t in pd.read_html(io.StringIO(html)):
        cols = [str(c) for c in t.columns]
        tick = next((c for c in cols if c in ("Ticker", "Symbol")), None)
        name = next((c for c in cols if c in ("Company", "Security")), None)
        if tick and name and 90 <= len(t) <= 110:
            return [(str(s).replace(".", "-"), str(n), "US") for s, n in zip(t[tick], t[name])]
    raise RuntimeError("找不到 Nasdaq-100 成分股表格")


def universe_nasdaq_all():
    txt = _get("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt")
    df = pd.read_csv(io.StringIO(txt), sep="|")
    df = df[df["Symbol"].notna() & ~df["Symbol"].astype(str).str.startswith("File Creation")]
    df = df[(df["Test Issue"] == "N") & (df["ETF"] == "N")]
    df = df[~df["Symbol"].astype(str).str.contains(r"[\$\.\^]")]
    # 排除權證、單位、特別股等
    bad = r"Warrant|Unit|Right|Preferred|Depositary Share|Notes"
    df = df[~df["Security Name"].astype(str).str.contains(bad, case=False)]
    return [(str(s), str(n).split(" - ")[0], "US") for s, n in zip(df["Symbol"], df["Security Name"])]


def universe_tw():
    out = []
    for mode, suffix in ((2, ".TW"), (4, ".TWO")):  # 2 = 上市, 4 = 上櫃
        html = _get(f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}", encoding="cp950")
        df = pd.read_html(io.StringIO(html), header=0)[0]
        df = df[df["CFICode"] == "ESVUFR"]  # 普通股
        for v in df.iloc[:, 0].astype(str):
            parts = v.replace(" ", "\u3000").split("\u3000", 1)
            if len(parts) == 2 and parts[0].isdigit():
                out.append((parts[0] + suffix, parts[1].strip(), "TW"))
    return out


def universe_etf():
    return [(t, n, "ETF") for t, n in GLOBAL_ETFS.items()]


# ──────────────────────────────────────────────────────────────
# 下載
# ──────────────────────────────────────────────────────────────
def download(tickers, batch=80, pause=1.5):
    import yfinance as yf
    data = {}
    todo = list(tickers)
    for attempt in range(2):
        failed = []
        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            print(f"  下載 {i + 1}–{i + len(chunk)} / {len(todo)}", file=sys.stderr)
            try:
                raw = yf.download(chunk, period="3y", interval="1d", group_by="ticker",
                                  auto_adjust=True, threads=True, progress=False)
            except Exception as e:  # noqa: BLE001
                print(f"  批次失敗：{e}", file=sys.stderr)
                failed += chunk
                continue
            for t in chunk:
                try:
                    df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
                    df = df.dropna(subset=["Close"])
                    if len(df) >= 260:
                        data[t] = df
                    elif len(df) == 0:
                        failed.append(t)
                except KeyError:
                    failed.append(t)
            time.sleep(pause)
        if not failed or attempt == 1:
            break
        print(f"  重試 {len(failed)} 檔", file=sys.stderr)
        todo = failed
    return data


# ──────────────────────────────────────────────────────────────
# 階段判斷
# ──────────────────────────────────────────────────────────────
STAGE_NAMES = {1: "第一階段：底部整理", 2: "第二階段：上升", 3: "第三階段：頭部", 4: "第四階段：下跌"}


def stage_series(c, s50, s150, s200):
    """
    逐日判斷階段：
      200 日均線上揚：股價在 200 日均線之上 → 第二階段；
                      跌破 200 日均線，或跌破 150 日均線且 50 日均線下彎 → 第三階段（趨勢轉弱）
      200 日均線下彎：股價在 200 日均線之下 → 第四階段；站回 → 第一階段（打底）
      200 日均線走平：數個月前均線是上揚的 → 第三階段（頭部）；否則 → 第一階段（底部）
    """
    slope = s200 / s200.shift(SLOPE_WINDOW) - 1
    prior = s200.shift(PRIOR_SHIFT) / s200.shift(PRIOR_SHIFT + SLOPE_WINDOW) - 1
    rising, falling = slope > SLOPE_BAND, slope < -SLOPE_BAND
    above = c > s200
    weak = (c < s150) & (c < s50) & (s50 < s50.shift(10))
    stage = np.select(
        [rising & above & ~weak, rising & (~above | weak), falling & ~above, falling & above, prior > SLOPE_BAND],
        [2, 3, 4, 1, 3],
        default=1,
    )
    stage = pd.Series(stage, index=c.index).where(s200.notna())
    # 平滑：取近 5 日最常出現的階段，避免在門檻附近來回跳動
    sm = stage.rolling(5, min_periods=1).apply(lambda w: pd.Series(w).mode().iloc[-1], raw=True)
    return sm, slope


def analyze(df):
    c, v = df["Close"].astype(float), df["Volume"].astype(float)
    s50, s150, s200 = (c.rolling(n).mean() for n in (50, 150, 200))
    stage, slope = stage_series(c, s50, s150, s200)

    last = len(c) - 1
    close = c.iloc[-1]
    hi52, lo52 = c.iloc[-252:].max(), c.iloc[-252:].min()

    # 趨勢樣板八項條件（第 8 項 RS 稍後在同市場內排名後補上）
    tt = [
        bool(close > s150.iloc[-1] and close > s200.iloc[-1]),   # 1 股價在 150、200 日均線之上
        bool(s150.iloc[-1] > s200.iloc[-1]),                     # 2 150 日均線在 200 日均線之上
        bool(s200.iloc[-1] > s200.iloc[-22]),                    # 3 200 日均線至少上揚一個月
        bool(s50.iloc[-1] > s150.iloc[-1] and s50.iloc[-1] > s200.iloc[-1]),  # 4 50 日均線在兩者之上
        bool(close > s50.iloc[-1]),                              # 5 股價在 50 日均線之上
        bool(close >= 1.30 * lo52),                              # 6 比 52 週低點高 30% 以上
        bool(close >= 0.75 * hi52),                              # 7 距 52 週高點 25% 以內
    ]

    # 第二階段持續天數：距最近一次處於第一或第四階段的天數
    st = stage.values
    cur = int(st[-1]) if not math.isnan(st[-1]) else 1
    days2, prev_stage = None, None
    if cur == 2:
        days2 = 0
        for k in range(last, -1, -1):
            if math.isnan(st[k]):
                break
            if st[k] in (1, 4):
                prev_stage = int(st[k])
                break
            days2 += 1

    emerging = bool(cur == 2 and days2 is not None and days2 <= EMERGING_MAX_DAYS
                    and prev_stage == 1 and close >= EMERGING_NEAR_HIGH * hi52)

    # RS 原始分數（IBD 式加權報酬）
    def roc(n):
        return c.iloc[-1] / c.iloc[-1 - n] - 1 if len(c) > n else np.nan
    rs_raw = 0.4 * roc(63) + 0.2 * roc(126) + 0.2 * roc(189) + 0.2 * roc(252)

    # 波動收縮與量縮（VCP 參考）
    r = c.pct_change()
    contraction = float(r.iloc[-10:].std() / r.iloc[-50:].std()) if r.iloc[-50:].std() > 0 else None
    vol_ratio = float(v.iloc[-10:].mean() / v.iloc[-50:].mean()) if v.iloc[-50:].mean() > 0 else None

    # 週線圖資料（如書中 BigCharts 週線：30、40 週均線）
    wk = c.resample("W-FRI").last().dropna()
    m30, m40 = wk.rolling(30).mean(), wk.rolling(40).mean()
    wst = stage.resample("W-FRI").last().reindex(wk.index)
    # 每個週線點實際對應的最後交易日（整週休市的週會被 dropna 剔除，
    # 若用「起始日 + 7 天」推算日期會錯位，故另外輸出）
    cv = c.dropna()
    wdt = pd.Series(cv.index, index=cv.index).resample("W-FRI").last().reindex(wk.index)
    wk, m30, m40, wst, wdt = (x.iloc[-CHART_WEEKS:] for x in (wk, m30, m40, wst, wdt))

    def rnd(x, d=4):
        return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), d)

    return {
        "close": rnd(close, 4),
        "chg_1d": rnd(c.iloc[-1] / c.iloc[-2] - 1),
        "date": c.index[-1].strftime("%Y-%m-%d"),
        "stage": cur,
        "days_in_stage2": days2,
        "emerging": emerging,
        "tt": tt,
        "rs_raw": rnd(rs_raw, 6),
        "from_high": rnd(close / hi52 - 1),
        "from_low": rnd(close / lo52 - 1),
        "slope200": rnd(slope.iloc[-1], 5),
        "contraction": rnd(contraction, 3),
        "vol_ratio": rnd(vol_ratio, 3),
        "avg_vol50": rnd(v.iloc[-50:].mean(), 0),
        "w": {
            "start": wk.index[0].strftime("%Y-%m-%d"),
            "d": [x.strftime("%Y-%m-%d") for x in wdt],
            "c": [rnd(x, 3) for x in wk],
            "m30": [rnd(x, 3) for x in m30],
            "m40": [rnd(x, 3) for x in m40],
            "st": [None if math.isnan(x) else int(x) for x in wst],
        },
    }


def finalize(rows):
    """在各市場內把 RS 轉成 1–99 的百分位，補上趨勢樣板第 8 項。"""
    df = pd.DataFrame([{"i": i, "m": r["market"], "rs": r["rs_raw"]} for i, r in enumerate(rows)])
    df["rank"] = df.groupby("m")["rs"].rank(pct=True)
    for i, p in zip(df["i"], df["rank"]):
        rs = None if pd.isna(p) else int(round(1 + p * 98))
        rows[i]["rs"] = rs
        rows[i]["tt"] = rows[i]["tt"] + [bool(rs is not None and rs >= 70)]
        rows[i]["tt_score"] = sum(rows[i]["tt"])
        del rows[i]["rs_raw"]
    return rows


# ──────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="《超級績效》四階段掃描")
    ap.add_argument("--markets", nargs="+", default=["sp500", "nasdaq", "tw", "etf"],
                    choices=["sp500", "nasdaq", "tw", "etf"])
    ap.add_argument("--nasdaq-all", action="store_true", help="掃描全部 Nasdaq 普通股，而非 Nasdaq-100")
    ap.add_argument("--min-avg-volume", type=float, default=0, help="排除 50 日均量低於此值的標的")
    ap.add_argument("--limit", type=int, default=0, help="每個市場只取前 N 檔（試跑用）")
    ap.add_argument("--out", default="stage_scan")
    a = ap.parse_args()

    loaders = {
        "sp500": universe_sp500,
        "nasdaq": universe_nasdaq_all if a.nasdaq_all else universe_nasdaq100,
        "tw": universe_tw,
        "etf": universe_etf,
    }
    uni = {}
    for m in a.markets:
        print(f"取得清單：{m}", file=sys.stderr)
        try:
            items = loaders[m]()
        except Exception as e:  # noqa: BLE001
            print(f"  無法取得 {m} 清單：{e}", file=sys.stderr)
            continue
        if a.limit:
            items = items[:a.limit]
        for t, n, mk in items:
            uni.setdefault(t, (n, mk))
    print(f"共 {len(uni)} 檔，開始下載…", file=sys.stderr)

    data = download(list(uni))
    rows = []
    for t, df in data.items():
        try:
            res = analyze(df)
        except Exception as e:  # noqa: BLE001
            print(f"  {t} 分析失敗：{e}", file=sys.stderr)
            continue
        if res["avg_vol50"] is not None and res["avg_vol50"] < a.min_avg_volume:
            continue
        name, market = uni[t]
        rows.append({"ticker": t, "name": name, "market": market, **res})

    if not rows:
        sys.exit("沒有取得任何資料（可能是 Yahoo Finance 暫時限制下載），本次不輸出。")
    rows = finalize(rows)
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "Yahoo Finance via yfinance",
        "params": {"slope_window": SLOPE_WINDOW, "slope_band": SLOPE_BAND,
                   "emerging_max_days": EMERGING_MAX_DAYS, "emerging_near_high": EMERGING_NEAR_HIGH},
        "count": len(rows),
        "stocks": rows,
    }
    import os
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out + ".json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    flat = pd.DataFrame([{k: v for k, v in r.items() if k not in ("w", "tt")} for r in rows])
    if len(flat):
        flat["stage_name"] = flat["stage"].map(STAGE_NAMES)
        flat.sort_values(["emerging", "stage", "rs"], ascending=[False, True, False]).to_csv(
            a.out + ".csv", index=False, encoding="utf-8-sig")

    counts = pd.Series([r["stage"] for r in rows]).value_counts().sort_index()
    print("\n完成。", file=sys.stderr)
    for s, n in counts.items():
        print(f"  {STAGE_NAMES[s]}：{n} 檔", file=sys.stderr)
    print(f"  正在起漲：{sum(r['emerging'] for r in rows)} 檔", file=sys.stderr)
    print(f"輸出：{a.out}.json、{a.out}.csv", file=sys.stderr)


if __name__ == "__main__":
    main()
