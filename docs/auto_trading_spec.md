# 台股自動交易規格書（草案 v0.1）

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
- 強制平倉：到 `force_close` 時間（預設 13:15）一律以市價或漲跌停價平倉。
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

## 4. 送單方式

- `order_confirm: manual`（預設）：程式產生「建議委託單」，通知使用者後等待確認才送出。超過 `confirm_timeout_sec` 未確認就自動取消。
- `order_confirm: auto`：符合條件即直接送單，不需確認。開啟時，啟動時會再提示一次風險。
- 兩種方式都受以下限制：
  - `max_daily_loss_pct`：當天已實現加未實現虧損達上限，停止開新倉。
  - `max_orders_per_day`：每天最多送單筆數。
  - 緊急停止：建立 `STOP` 檔案（或按下程式的停止鍵）後立即停止開新倉，並依設定選擇是否平掉所有當沖部位。

## 5. 價格檔位（台股現股升降單位）

| 股價 | 每檔 |
|---|---|
| 未滿 10 元 | 0.01 |
| 10 ～ 未滿 50 元 | 0.05 |
| 50 ～ 未滿 100 元 | 0.1 |
| 100 ～ 未滿 500 元 | 0.5 |
| 500 ～ 未滿 1000 元 | 1 |
| 1000 元以上 | 5 |

## 6. 程式架構（規劃）

```
trader/
  config.example.yaml   設定檔範本（使用者複製成 config.yaml，不進版控）
  universe.py           讀 stage_scan.json、篩選、排除不可交易標的
  strategy_daytrade.py  開盤區間突破
  strategy_swing.py     超級績效波段規則
  risk.py               部位大小、每日虧損、緊急停止
  broker_kgi.py         SuperPy 包裝（登入、報價、下單、查詢）← 需 API 文件
  broker_paper.py       模擬下單（回測、只通知模式用）
  notifier.py           通知與確認（LINE／Telegram）
  journal.py            交易日誌
  main.py               主程式
backtest/               回測
```

- 交易程式在使用者自己的 Windows 電腦上執行；帳號、密碼、憑證只放本機，絕不提交到公開儲存庫。
- `broker_paper.py` 與 `broker_kgi.py` 介面相同，切換只改設定。

## 7. 待確認事項

1. SuperPy 的函式：登入、憑證、即時報價訂閱、現股與當沖下單（含先賣後買的委託條件）、改單、刪單、委託與成交回報、庫存查詢。
2. 是否提供「可否當沖」「處置股」「漲跌停價」的資料，或需要另外取得。
3. 零股能否當沖。
4. 當沖額度的查詢方式。
5. 確認通知要用 LINE 還是 Telegram。
