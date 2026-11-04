# 台股自動交易規格書（草案 v0.6）

搭配 `stage_scan.json`（每日 6:30 產生的階段掃描結果），透過凱基 SuperPy 下單。
兩種模式並存：**波段模式**（依《超級績效》規則）與**當沖模式**（現股當沖，開盤區間突破）。

> 本工具只執行使用者設定的規則，不保證獲利。程式錯誤、斷線、滑價、漲跌停都可能造成損失。
> 上線順序：回測 → 只通知 → 確認後送單（預設）→ 全自動（選用）。

---

## 1. 選股範圍（兩種模式共用）

1. 讀取當天的 `stage_scan.json`，只取台股（`market` 為台股的標的）。
2. 依設定篩選：
   - `stages`：要交易的階段，例如 `[2]`、`[4]`、`[1, 2]`。
   - `trend_template`：是否要求趨勢樣板八項全過。
   - `min_rs`：RS 最低門檻。
   - `min_avg_volume_lots`：50 日均量最少幾張。
3. 每天開盤前排除：處置股、全額交割股、暫停交易、不可當沖（當沖模式），以及當天已觸發 `max_daily_loss` 的狀態。
4. 資料過期保護：`stage_scan.json` 的 `generated_at` 若不是當天產生，當天不開新倉，只管理既有部位。

## 2. 當沖模式（現股當沖）

### 2.1 方向
- `long`：先買後賣。
- `short`：先賣後買（需已開通當沖資格，且當天必須回補）。

### 2.2 開盤區間突破（Opening Range Breakout）
1. 開盤區間：09:00 起算 `opening_range_minutes` 分鐘（預設 15，即 09:00–09:15），記錄區間最高價 `ORH`、最低價 `ORL`、累計成交量。
2. 區間寬度過濾：`(ORH − ORL) / ORL` 必須介於 `min_range_pct` 與 `max_range_pct` 之間，太窄容易假突破，太寬則停損太遠。
3. 進場條件（只在 `entry.start`～`entry.end` 之間）：
   - 做多：成交價 ≥ `ORH` 加一檔（tick）。
   - 做空：成交價 ≤ `ORL` 減一檔。
   - 量能：開盤區間累計量 ≥ 50 日均量 × `or_volume_ratio`（選用）。
   - 距漲跌停不足 `limit_buffer_pct` 時不進場（避免漲停買不回、跌停賣不掉）。
4. 每檔每天最多進場 `max_entries_per_symbol` 次（預設 1）。

### 2.3 出場（先到先出）
- 停損：做多為 `max(ORL, 進場價 × (1 − stop_loss_pct))`，做空為 `min(ORH, 進場價 × (1 + stop_loss_pct))`，取較近者。
- 停利：進場價 ± `take_profit_pct`；或以風險倍數 `take_profit_r` 計算（兩者擇一，可不設）。
- 強制平倉：到 `force_close` 時間（預設 13:10）一律以市價或漲跌停價平倉。
- 先賣後買若回補失敗（例如漲停鎖死），立即通知使用者，並持續掛單回補直到收盤。

### 2.4 部位大小
- 單筆金額上限：`max_amount_per_trade`（使用者自設，單位：元，例如 300000 為 30 萬）。
- 風險上限：單筆最大虧損 ≤ 總資金 × `risk_per_trade_pct`。
- 張數 = `floor(min(金額上限 ÷ (股價 × 1000), 風險上限 ÷ ((進場價 − 停損價) × 1000)))`，小於 1 張就不進場。

## 3. 波段模式（依《超級績效》）

### 3.1 候選條件
- 第二階段，且趨勢樣板八項全過（預設），RS ≥ `min_rs`（預設 70）。
- 波動收縮：`contraction` ≤ `max_contraction`（近 10 日波動 ÷ 近 50 日波動，預設 0.8）。
- 量縮：`vol_ratio` ≤ `max_vol_ratio`（近 10 日均量 ÷ 近 50 日均量，預設 1.0）。

### 3.2 樞紐點與進場
- 樞紐價 `pivot`：近 `pivot_lookback` 個交易日（預設 20）的最高價。
- 進場條件：
  - 成交價突破 `pivot`。
  - 突破當日預估量 ≥ 50 日均量 × `breakout_volume_ratio`（預設 1.4）。
  - 不追高：進場價 ≤ `pivot × (1 + max_chase_pct)`（預設 5%），超過就放棄。
- 進場時機 `entry_timing`：
  - `intraday`：盤中突破即進場。
  - `next_open`：收盤確認突破後，隔天開盤進場。

### 3.3 出場規則
- 初始停損：進場價 × (1 − `stop_loss_pct`)，預設 7%，上限 8%。
- 保本：獲利達停損距離的 `breakeven_r` 倍（預設 2 倍）後，停損上移到成本價。
- 趨勢破壞：收盤跌破 50 日均線，且當日量 ≥ 50 日均量 × `ma_break_volume_ratio`，隔天出場。
- 分批獲利（選用）：獲利達 `partial_take_pct`（例如 20%）時賣出 `partial_take_size`（例如一半）。

### 3.4 部位大小
- 每筆風險 ≤ 總資金 × `risk_per_trade_pct`（預設 1%）。
- 單一個股 ≤ 總資金 × `max_position_pct`（預設 25%）。
- 同時持有 ≤ `max_positions` 檔。
- 整張或零股（`lot_mode`）：
  - `lot`（預設）：只買整張。高價股可能連一張都買不起而略過。
  - `odd`：依風險規則算出的股數直接買，例如 2330 買 80 股。整張部分下整股單（`OddLot.Common`），不足一張的部分下盤中零股單（`OddLot.Odd`），兩筆分開計費、分開追蹤，出場也各自用原本的方式下單。
  - 盤中零股的成交量較少、價差較大，回測未模擬這部分成本。

## 4. 送單方式

- `order_confirm: manual`（預設）：程式產生「建議委託單」，在本機程式視窗等待使用者確認才送出（同時發 LINE 通知提醒）。超過 `confirm_timeout_sec` 未確認就自動取消。
- `order_confirm: auto`：符合條件即直接送單，不需確認。開啟時，啟動時會再提示一次風險。
- 兩種方式都受以下限制：
  - `max_daily_loss_pct`：當天已實現加未實現虧損達上限，停止開新倉。
  - `max_orders_per_day`：每天最多送單筆數。
  - 緊急停止：建立 `STOP` 檔案（或按下程式的停止鍵）後立即停止開新倉，並依設定選擇是否平掉所有當沖部位。

### 4.1 LINE 通知（只通知，不在 LINE 確認）

- 使用 **LINE Messaging API** 推播（LINE Notify 已於 2025 年 3 月停止服務）。
- **使用者負責**：建立 LINE 官方帳號與 Messaging API channel、加機器人為好友，取得兩個值，設成環境變數或寫進 `trader/.env`（不會提交）：
  - `LINE_CHANNEL_ACCESS_TOKEN`：Channel access token
  - `LINE_TO`：接收通知的 LINE 使用者 ID（U 開頭）
- 程式只負責推播，不需要 webhook，也不接收 LINE 的回覆。
- 送單確認一律在本機程式視窗按下（`order_confirm: manual`）。
- 通知項目可個別開關：進場訊號、成交、出場、錯誤、每日總結（`notify.on_*`）。
- 推播有每月免費則數上限，內容保持精簡。

### 4.2 交易參數的修改與儲存

本規格書中的數值都是**預設值**，使用者可以隨時修改，修改後會保存，下次啟動沿用。

- `trader/defaults.yaml`：預設值（隨程式更新）。
- `trader/config.yaml`：使用者設定，**只記錄跟預設值不同的項目**，不提交到儲存庫。預設值日後更新時，你沒改過的項目會自動用新的預設值。
- 每次儲存前整份檢查（型別、範圍、時間先後），不合法就不寫入，並保留上一版為 `config.yaml.bak`。
- 目前用指令修改；之後的交易程式視窗會有設定頁，使用同一套讀寫與檢查。

```bash
python -m trader.settings show                                  # 顯示全部設定，標出已修改項目
python -m trader.settings set daytrade.exit.take_profit_pct 5   # 修改並儲存
python -m trader.settings reset daytrade.exit.take_profit_pct   # 單項改回預設
python -m trader.settings reset                                 # 全部改回預設
```

主要檢查規則：
- 時間格式 HH:MM；進場開始不早於開盤區間結束；進場開始 < 進場結束 < 強制平倉 ≤ 13:25。
- 波段停損上限 8%（書中規則）；停損不能不設。
- 選項類（模式、方向、券商環境、確認方式）只接受列出的值；階段只能是 1～4。
- 「正式環境 + 全自動免確認」可以設定，但每次修改與啟動都會顯示警告。

## 5. 價格檔位（台股現股升降單位）

| 股價 | 每檔 |
|---|---|
| 未滿 10 元 | 0.01 |
| 10 ～ 未滿 50 元 | 0.05 |
| 50 ～ 未滿 100 元 | 0.1 |
| 100 ～ 未滿 500 元 | 0.5 |
| 500 ～ 未滿 1000 元 | 1 |
| 1000 元以上 | 5 |

## 6. 凱基 SuperPy 對應（依手冊 v1.37 整理）

參考來源：社群維護的 [kgi-superpy-tw](https://github.com/pypysuper-og/kgi-superpy-tw) skill（依凱基 Python API 使用手冊 v1.37 整理，非官方文件）。實際開發時以安裝的 SDK 版本與[官方文件](https://superpy.kgieworld.com.tw/kgipythonapi/index)為準。

### 6.1 啟動流程

1. `api = kgi.login(person_id, person_pwd, simulation=True)`：`True` 是模擬環境（預設），`False` 才是正式環境。程式絕不自動從模擬切換到正式。
2. `api.show_account()` → 依設定檔指定的證券帳號 `api.set_Account(account)`（不取清單第一個）。
3. `api.Order.set_event(on_order_event)`：**送任何單之前**先註冊委託回報。
4. `api.set_disconnect_cb(on_disconnect)`：斷線處理（見 6.6）。
5. 盤前讀 `api.Order.contract("df")`：取得漲跌停價、參考價、**可否當沖**，用來做第 1 節的排除與第 2.2 節的漲跌停保護。
6. 收盤後停用自動交易、確認無未結委託，再 `api.logout()`。

### 6.2 報價

- 先設回呼再訂閱：`api.Quote.set_cb_tick(...)`、`set_cb_kbar(...)`、`set_cb_event(...)`。
- 開盤區間與突破判斷：`api.Quote.subscribe_tick(symbol, odd_lot=False)`；也可用 `subscribe_kbar(symbol, 1)` 取 1 分 K。
- **訂閱數量有上限**（手冊：新星 2 連線 × 30 檔、菁英 3 × 50、尊爵 9 × 100，以實際權限為準）。因此觀察名單需設 `max_watchlist` 上限，依 RS 排序取前 N 檔。
- 回呼內只做「放進佇列」，策略運算在另一個執行緒，避免 `CALLBACK_SLOW`、`DROPPED_MESSAGE`。
- 整股與零股報價分開，不互相替代。

### 6.3 下單

```python
trade = api.Order.create_order(
    action=kgi.Action.Buy,            # Buy / Sell
    symbol="2330",
    qty=1,                            # 整股：手冊範例顯示為「張」，上線前需實測確認
    price=612.0,                      # 或 kgi.PriceType.MKT / LimitUp / LimitDown
    time_in_force=kgi.TimeInForce.ROD,  # ROD / IOC / FOK
    order_cond=kgi.OrderCond.CASH,    # 現股
    odd_lot=kgi.OddLot.Common,        # 整股
    name="dt-orb",                    # 策略代號，方便對帳
)
```

| 動作 | 對應（待模擬環境驗證） |
|---|---|
| 當沖先買後賣 | 買進 `CASH`，當天賣出 `CASH` |
| 當沖先賣後買 | 賣出 `CASH_SELLING`（推測為現股當沖賣出），當天買回 `CASH` |
| 波段買進／賣出 | `CASH` |
| 改價 | `update_order(order_id, price=...)`（市價單不能改價） |
| 減量 | `update_order(order_id, qty=N)`：**是減少 N 張，不是改成 N 張** |
| 刪單 | `cancel_order(order_id)`；緊急停止用 `cancel_order_all()` |

- 手冊回傳物件中另有 `MARGIN_DayTrade`、`SHORT_DayTrade`，但不在下單可用參數內，不使用。
- `CASH_SELLING` 的確切意義手冊未說明清楚，**先賣後買上線前必須在模擬環境驗證，並向凱基確認**。

### 6.4 委託狀態判讀

- `create_order` 回傳 `Trade` ≠ 委託成功 ≠ 成交。三者分開記錄：
  - `Task.NewOrder` + `Status.Pending`：系統收到，尚未完成。
  - `Task.NewOrder` + `Status.Success`：新單成功。
  - `Task.Deal`：成交（價格、數量）。
  - `Status.Failed`：失敗，記錄 `msg`。
- **結果不明時絕不自動重送**，改用 `api.Account.OrderReport()`、`ExecReport()`、`api.Order.get_trades(True)` 對帳，仍不明就通知使用者。
- 部位以成交回報為準，定時用 `api.Order.get_position()`、`api.Account.InventorySum("A")` 核對。

### 6.5 回測資料

- 日 K：`api.Data.get("還原日K價量資料(個股、ETF、大盤)-單檔股票多個區間", symbol)`。
- 分 K：`取得歷史分K(指定日期前)`、`取得歷史1分K(指定日期前5天)` 等；手冊有使用者遇到歷史分 K 權限被拒（`D403`），需先確認帳號權限。
- 可用 SDK 內建 `api.backtest(...)`（台股當沖稅率預設 0.0015、手續費 0.00025），或自行撰寫回測。

### 6.6 斷線恢復

重新登入不等於恢復完成。恢復時依序：重新 `set_Account` → 重新註冊委託回報 → 重新訂閱報價 → 對帳委託、成交、庫存 → 確認報價時間是新的 → 才恢復策略。斷線期間不開新倉；對帳有差異就停止並通知使用者。

## 7. 程式架構（規劃）


```
trader/
  defaults.yaml         交易參數預設值                       ✅
  settings.py           參數讀取、檢查、儲存                 ✅
  twse.py               升降單位、漲跌停、手續費與證交稅     ✅
  universe.py           讀 stage_scan.json 產生觀察名單      ✅
  daytrade.py           當沖：開盤區間突破                   ✅
  swing.py              波段：超級績效規則（日 K 投資組合）  ✅
  paper_broker.py       程式內模擬下單（介面比照 SuperPy）   ✅
  data.py               行情資料（yfinance、CSV）            ✅
  broker_kgi.py         SuperPy 包裝（登入、報價、下單、對帳）
  notifier.py           LINE 推播
  journal.py            交易日誌
  main.py               主程式（盤中執行、本機確認視窗）
backtest/run.py         回測指令與報告                       ✅
tests/                  測試                                 ✅
```

- 交易程式在使用者自己的 Windows 電腦上執行；帳號、密碼、憑證只放本機，絕不提交到公開儲存庫。
- `broker_paper.py` 與 `broker_kgi.py` 介面相同，切換只改設定。

## 8. 待確認事項（需在模擬環境驗證或向凱基詢問）

1. 先賣後買是否使用 `OrderCond.CASH_SELLING`。
2. 整股 `qty` 的單位是否為「張」（手冊範例支持，但未明文保證）。
3. 你的帳號等級可訂閱幾檔報價（決定 `max_watchlist`）。
4. 歷史分 K 資料權限（影響回測）。
5. 處置股、全額交割股是否能從 `contract()` 或 `TSECreditInfo()` 判斷。
6. 當沖額度的查詢方式。
