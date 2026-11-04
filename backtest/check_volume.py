"""檢查 Yahoo 台股分 K 的成交量是否可信。

    python -m backtest.check_volume 2317 2303

比較每天「分 K 成交量加總」與「日 K 成交量」，並列出開盤前 15 分鐘占全日的比例。
兩者應該接近（比例約 1）；差很多就代表分 K 成交量不能直接拿來跟日均量比。
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trader import data as D  # noqa: E402


def main(argv=None):
    symbols = (argv if argv is not None else sys.argv[1:]) or ["2317"]
    for s in symbols:
        intr = D.from_yfinance([s], "5m").get(s)
        daily = D.from_yfinance([s], "1d").get(s)
        if intr is None or daily is None:
            print(f"{s}：抓不到資料")
            continue
        day = intr.index.normalize()
        first = intr[intr.index.strftime("%H:%M") < "09:15"]
        t = pd.DataFrame({
            "分K加總（張）": intr.groupby(day)["volume"].sum() / 1000,
            "日K（張）": daily["volume"] / 1000,
            "前15分鐘（張）": first.groupby(first.index.normalize())["volume"].sum() / 1000,
            "第一根K棒時間": intr.groupby(day).apply(lambda x: x.index.min().strftime("%H:%M")),
        }).dropna()
        t["分K÷日K"] = (t["分K加總（張）"] / t["日K（張）"]).round(2)
        t["前15分鐘占全日"] = (t["前15分鐘（張）"] / t["日K（張）"]).map("{:.0%}".format)
        print(f"\n=== {s}（最近 10 天）===")
        print(t.tail(10).round(0).to_string())
        print(f"分K÷日K 中位數：{t['分K÷日K'].median():.2f}（接近 1 才正常）")


if __name__ == "__main__":
    main()
