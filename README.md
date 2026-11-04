# 階段掃描

依《超級績效》（Mark Minervini）的四階段與趨勢樣板，每個交易日自動掃描美股（S&P 500、Nasdaq-100）、台股上市櫃與全球主要 ETF，並把結果發布成網頁。

- `stage_scanner.py`：掃描程式
- `index.html`：網頁，打開時自動讀取最新的 `stage_scan.json`
- `.github/workflows/scan.yml`：每個交易日早上 6:30（台灣時間）自動執行

本專案僅為技術面篩選工具，不構成投資建議。
