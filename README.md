# 階段掃描

依《超級績效》（Mark Minervini）的四階段與趨勢樣板，每個交易日自動掃描美股（S&P 500、Nasdaq-100）、台股上市櫃與全球主要 ETF，並把結果發布成網頁。

- `stage_scanner.py`：掃描程式
- `index.html`：網頁，打開時自動讀取最新的 `stage_scan.json`
- `.github/workflows/scan.yml`：每個交易日早上 6:30（台灣時間）自動執行

本專案僅為技術面篩選工具，不構成投資建議。

## 自動交易（開發中）

規格書：[`docs/auto_trading_spec.md`](docs/auto_trading_spec.md)。目前完成**參數設定、回測、程式內模擬下單**；凱基 SuperPy 下單尚未接上。

### 安裝

```bash
pip install pandas numpy pyyaml yfinance
```

### 交易參數

預設值在 `trader/defaults.yaml`，你的修改存在 `trader/config.yaml`（不會上傳）。

```bash
python -m trader.settings show
python -m trader.settings set daytrade.exit.take_profit_pct 5
python -m trader.settings reset daytrade.exit.take_profit_pct
```

### 回測

需在可連 Yahoo Finance 的電腦執行（或改用 `--source csv` 讀自己的資料）。Yahoo 的台股分 K 只有最近約 60 天（5 分 K），當沖回測期間較短。

```bash
python -m backtest.run daytrade --symbols 2330 2454 3008 2317
python -m backtest.run swing --symbols 2330 2454 3008 2317 2308 2382 3711 2603 2881 1301 --start 2023-01-01
python -m backtest.run daytrade --symbols 2330 --set daytrade.exit.take_profit_pct=4
```

`--set` 只影響這次回測，不會儲存。結果存在 `backtest/results/`（`summary.txt`、`trades.csv`）。

### 測試

```bash
python tests/test_settings.py
python tests/test_trading.py
```
