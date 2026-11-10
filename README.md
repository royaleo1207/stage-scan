# 階段掃描

依《超級績效》（Mark Minervini）的四階段與趨勢樣板，每個交易日自動掃描美股（S&P 500、Nasdaq-100）、台股上市櫃與全球主要 ETF，並把結果發布成網頁。

- `stage_scanner.py`：掃描程式
- `index.html`：網頁，打開時自動讀取最新的 `stage_scan.json`
- `.github/workflows/scan.yml`：每個交易日早上 6:30（台灣時間）自動執行
- `trader/order_server.py`：本機下單程式（凱基 SuperPy，台股＋美股複委託），見下方「自由下單」

本專案僅為技術面篩選工具，不構成投資建議。

## 自由下單（凱基 SuperPy：台股＋美股複委託）

在網頁勾選標的、自由設定委託單與出場規則，送到你 Windows 電腦上的「本機下單程式」，由它透過凱基 SuperPy（`kgisuperpy` 2.1.2）下單、監控停損停利並自動平倉。

```
網頁 index.html ──POST /plan（配對碼）──▶ 本機下單程式 127.0.0.1:8765 ──▶ 凱基 SuperPy ──▶ 凱基
                                               │  訂閱 tick、監控出場規則、風控
                                               └─ 狀態頁 http://127.0.0.1:8765/
```

> 本工具只執行你設定的規則，不保證獲利。程式錯誤、斷線、滑價、漲跌停鎖死都可能造成損失。請先用模擬環境測試。

### 1. 安裝 Python

1. 到 <https://www.python.org/downloads/windows/> 下載 **64 位元** Python 3.11～3.14（凱基手冊：64 位元 Windows 10 以上、Python 3.9～3.14）。
2. 安裝時勾選 **Add python.exe to PATH**。
3. 開「命令提示字元」，進到專案資料夾（`cd C:\路徑\stage-scan`），安裝套件：

```bash
python -m pip install -U kgisuperpy pandas numpy pyyaml tzdata
```

`tzdata` 是 Windows 計算美東夏令／冬令時間需要的時區資料，不能省略。

正式環境另外需要：凱基證券帳戶、簽署「證券 API 服務風險預告書暨使用同意書」、有效的 CA 憑證（依凱基說明安裝憑證元件並申請）。複委託 API 需另外向營業員申請開通。

### 2. 設定 .env（帳密只放這裡）

```bash
copy trader\.env.example trader\.env
notepad trader\.env
```

| 項目 | 說明 |
| --- | --- |
| `KGI_PERSON_ID`、`KGI_PERSON_PWD` | 凱基登入的身分證字號與密碼 |
| `KGI_TW_ACCOUNT` | 證券帳號（台股）。不用台股可留空 |
| `KGI_US_ACCOUNT` | 複委託帳號（美股）。不用美股可留空 |
| `PAIR_CODE` | 選填。固定配對碼；不填則每次啟動隨機產生 6 位數 |

`trader/.env`、`trader/runtime/`（委託與紀錄）、`trader/order_config.yaml`（你的風控設定）都在 `.gitignore` 裡，不會提交到 GitHub。不知道帳號時先留空執行一次，程式會列出登入帳號清單（程式不會自動選第一個帳號）。

### 3. 用模擬帳號測試

> **帳號不能用模擬環境登入時**（有些帳號凱基只開放正式環境），改用「演練模式」：
>
> ```bash
> python -m trader.order_server --dry-run
> ```
>
> 用正式環境登入，讀取**真實**的行情、庫存、商品資料、可否當沖與信用額度，下單前檢查也與正式相同（含交易時段），
> 但台股與美股的委託**一律在本機模擬成交，不會送到凱基**。網頁與狀態頁會顯示「演練模式」。以下步驟相同，只是第 1 步改用 `--dry-run`。
> 演練的成交是用最新成交價模擬，不含排隊、部分成交與漲跌停鎖死；下單程式重啟後，演練中的未成交委託會失效。
> 正式登入需要有效的 CA 憑證，且請在交易時段測試（盤後沒有 tick，出場規則不會觸發）。

1. 啟動本機下單程式（預設 `simulation=True`，凱基模擬環境）：

   ```bash
   python -m trader.order_server
   ```

   終端機會顯示模式、帳號、狀態頁網址與 **配對碼**。
2. 用 Chrome 或 Edge 開啟階段掃描網頁（GitHub Pages 或本機 `index.html`）。在「掃描結果」勾選台股或美股標的。
3. 到「下單面板」輸入配對碼、按「連線」，應顯示「模擬模式」。
4. 每檔設定委託單；需要的話勾「加掛出場規則」。
5. 按「送出到下單程式」→ 確認視窗會列出每張委託的完整內容、預估金額與模擬／正式模式 → 勾選確認後送出。
6. 開 <http://127.0.0.1:8765/> 看委託、成交、部位、損益與紀錄；測試「全部取消」「全部平倉」按鈕。
7. 到「我的庫存」按「重新整理」，試著對庫存「設定賣出」或「停損停利」。
8. 結束按 `Ctrl+C`。重新啟動後會接續監控（狀態存在 `trader/runtime/state.json`，所有動作記錄在 `trader/runtime/orders.log`）。

注意：
- **凱基沒有複委託（美股）模擬環境**。模擬模式下美股委託不會送到券商，而是由下單程式依即時報價在本機模擬成交，方便測試出場規則；若模擬帳號收不到美股行情，美股委託會一直停在「已委託」。
- 凱基台股模擬環境的服務時間為營業日 08:30～隔日 05:00，測試說明要求下單間隔大於 1 秒（程式在模擬環境自動間隔 1.1 秒）、測試時段 08:00～20:00 須使用台灣 IP。模擬環境預設不檢查交易時段，正式環境會檢查（可在 `order_config.yaml` 設 `enforce_hours_in_simulation: true`）。
- Chrome 會在第一次連線本機時詢問是否允許網站存取區域網路裝置，請選允許。Safari 不允許 https 網頁連本機，請改用 Chrome／Edge，或用「匯出計畫 JSON」。
- 用 `file://` 直接開 `index.html` 時，來源是 `null`，需在 `trader/order_config.yaml` 的 `allowed_origins` 加入 `"null"`。

### 4. 委託單選項

| 欄位 | 台股 | 美股（複委託） |
| --- | --- | --- |
| 買賣別 | 買進／賣出 | 買進／賣出（賣出限既有庫存） |
| 交易類別 | 現股、融資、融券、借券賣出、現股當沖先賣 | 只有現股 |
| 單位 | 整股（張）、盤中零股、盤後零股、定盤（盤後定價，張） | 股數（需為 board_lot 倍數） |
| 價格類型 | 限價、市價、漲停、跌停、平盤 | 只有限價 |
| 委託條件 | ROD、IOC、FOK | 只有 ROD |
| 其他 | | 交割幣別：外幣（MUT）／台幣（TWD） |

#### 各交易類別說明（台股）

| 交易類別 | 開倉 | 部位方向 | 程式自動選的平倉方式 | SuperPy 參數 |
| --- | --- | --- | --- | --- |
| 現股 | 買進 | 多 | 現股賣出 | `OrderCond.CASH` |
| 融資 | 融資買進 | 多 | 融資賣出 | `OrderCond.MARGIN` |
| 融券 | 融券賣出 | 空 | 融券買回 | `OrderCond.SHORT_SELLING` |
| 借券賣出 | 借券賣出（需有借券餘額） | 空 | 現股買回，之後需另行辦理還券 | `OrderCond.Lend_SELLING` |
| 現股當沖先賣 | 先賣（daytrade_short） | 空 | 當日以現股買進回補 | `OrderCond.CASH_SELLING`（回補用 `CASH` 買進，API 不接受 CASH_SELLING 買進） |
| 賣出擔保品 | — | — | — | **API 不支援，網頁停用** |

「賣出＋現股」「賣出＋融資」「買進＋融券」是平倉單（賣出庫存或回補），下單程式會先檢查庫存，且不能加出場規則。

#### 出場規則（可不加）

- **部位類型**：波段，或當沖（台股預設 13:20、美股收盤前 15 分鐘強制平倉；可用「出場時間」改）。
- **停損／停利**：可用 % 或價格。程式依部位方向自動判斷：多方跌破停損價、空方漲過停損價出場。
- **移動停利**：多方從最高價回落 X%、空方從最低價反彈 X% 出場。
- **進場時間窗、突破價進場**：設定後，委託先在程式內等待，時間窗內且價格突破（多方 ≥、空方 ≤）才送單；時間窗結束未觸發就過期，已送出未成交的部分會刪單。
- **出場時間、波段最長持有天數**：波段持有滿 N 個交易日，在出場時間平倉。
- 平倉單價格：台股整股以跌停價賣出／漲停價買回（ROD，等同立即成交）；零股與美股只能限價，以最新價往不利方向數檔（或 0.5%）掛單，20 秒未成交就刪單重掛（最多 5 次）。

#### 下單前檢查（不符合就拒絕並說明原因）

- 可否當沖：`Order.contract()` 的 `day_trade`（`Yes` 可先買後賣與先賣後買、`OnlyBuy` 只能先買後賣、`No` 不可當沖）。
- 融資融券可用額度：`Order.TSECreditInfo(代號)` 的 `MarignQty`、`ShortQty`。
- 庫存是否足夠：`Account.InventorySum('B')`（扣掉其他未成交的平倉單）；美股用 `SubAccount.StockPositionReport()`。
- 價格檔位、漲跌停範圍、交易時段（台股依台北時間；美股依美東時間，自動處理夏令／冬令）。
- 複委託：只接受美國市場商品、限價 ROD、股數為 board_lot 倍數、不能放空。

#### 風控（`trader/order_defaults.yaml`，要改就建立 `trader/order_config.yaml`）

| 項目 | 預設 | 說明 |
| --- | --- | --- |
| `risk.max_order_amount` | 300,000 | 單筆金額上限（台幣，美股以 `usd_twd` 換算） |
| `risk.max_daily_amount` | 2,000,000 | 單日新委託總金額上限 |
| `risk.max_daily_loss` | 30,000 | 單日最大虧損（已實現＋未實現）；觸發後停止新委託並平掉當沖部位 |
| `risk.max_short_amount` | 500,000 | 空方部位（融券、借券賣出、現股當沖先賣）總金額上限 |
| `risk.max_orders_per_day` | 50 | 單日新委託筆數上限 |

平倉單（停損停利、強制平倉、緊急平倉）不受金額上限限制。狀態頁的「全部取消」刪除本程式所有未成交委託；「全部平倉」把本程式監控中的部位全部平倉並停止今日新委託（都不會動到你在其他軟體下的單）。

### 5. 正式啟用前檢查清單

- [ ] 模擬環境（或 `--dry-run` 演練模式）已完整測過：買進、賣出、融資、融券、現股當沖先賣、零股、停損、停利、移動停利、當沖 13:20 平倉、全部取消、全部平倉、重啟後接續監控。
- [ ] `python tests/test_order_server.py` 全部通過。
- [ ] 已簽署 API 同意書、CA 憑證有效；複委託 API 已由營業員開通（美股無模擬環境，第一次請用最小股數測試）。
- [ ] `trader/.env` 的帳號正確（證券帳號、複委託帳號分開），`.env` 沒有被提交（`git status` 看不到它）。
- [ ] `trader/order_config.yaml` 的金額上限、單日虧損上限符合你的資金；`usd_twd` 匯率已更新。
- [ ] 已確認信用交易資格（融資融券、現股當沖、借券）都已向凱基開通。
- [ ] 了解限制：台股與美國的國定假日、美股提早收盤日沒有內建；斷線時程式不會自動重送不明的委託，請到狀態頁與券商軟體對帳。
- [ ] 電腦不會休眠、網路穩定；交易時間內下單程式保持開啟。
- [ ] 啟動正式環境：

  ```bash
  python -m trader.order_server --live
  ```

  終端機要求輸入 `YES`（大寫）才會啟動。網頁連線後會顯示紅色「正式模式」，確認視窗也會要求勾選「我了解這是正式下單」。

### 6. API 不支援、網頁上停用的功能

| 市場 | 功能 | 原因（kgisuperpy 2.1.2 原始碼） |
| --- | --- | --- |
| 台股 | 賣出擔保品 | `OrderCond` 沒有對應的值 |
| 台股 | 範圍市價 | `create_order` 傳入 `PriceType.RangeMarket` 直接報錯 |
| 美股 | 融資、融券、借券賣出、賣出擔保品、先賣後買 | `SubOrder.create_order` 沒有 `order_cond` 參數 |
| 美股 | 市價、範圍市價、漲停、跌停、平盤 | 複委託只接受限價單 |
| 美股 | IOC、FOK | `SubOrder.create_order` 沒有 `time_in_force`，固定 ROD |
| 美股 | 非美國市場（港股等） | SDK 只接受 `Market.US` |
| 美股 | 模擬環境 | 凱基手冊明載複委託沒有模擬環境（改為本機模擬成交） |

另外依交易所規則停用的組合：零股與盤後定價只能限價 ROD；融資、融券、借券賣出、現股當沖先賣只能整股；當沖只能盤中整股。

## 自動交易（開發中）

規格書：[`docs/auto_trading_spec.md`](docs/auto_trading_spec.md)。目前完成**參數設定、回測、程式內模擬下單**；策略自動選股下單尚未接上凱基（手動自由下單請用上方的本機下單程式）。

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

用整個台股市場當股票池（依 50 日均量取前 N 檔；階段與 RS 在回測中逐日判斷，不偷看未來）：

```bash
python -m backtest.run swing --scan https://royaleo1207.github.io/stage-scan/stage_scan.json --top 150 --start 2023-01-01 --set swing.lot_mode=odd
```

檢查 Yahoo 分 K 成交量是否可信：

```bash
python -m backtest.check_volume 2317 2303
```

`--set` 只影響這次回測，不會儲存。結果存在 `backtest/results/`（`summary.txt`、`trades.csv`）。

### 測試

```bash
python tests/test_settings.py
python tests/test_trading.py
python tests/test_order_server.py
```

`tests/test_order_server.py` 用假的 SuperPy 物件（`tests/fake_superpy.py`）測試下單程式，不會連線券商。修改 `trader/kgi_spec.py` 的選項表後，執行 `python -m trader.sync_caps` 同步到 `index.html`。
