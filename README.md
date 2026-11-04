# Agent 用量監控

Windows 桌面小工具，顯示三個 agent **已經用了多少 token**、**官方額度還剩多少**：

- Claude Code
- Codex
- Antigravity（agy）

## 安裝

需要 Windows（在 Windows 11 上實測）與 Python 3.12（含 tkinter）。

要查額度的 agent 必須已經在這台電腦登入：Claude 桌面 App 或 Claude Code、Codex、agy。沒有裝或沒有登入的那一家會在卡片上顯示原因，不影響其他家。

```bash
git clone https://github.com/qwe111370-del/agent-usage-monitor.git
```

```bash
cd agent-usage-monitor
```

```bash
py -3.12 -m venv .venv
```

```bash
.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
```

## 啟動

在專案資料夾內執行：

```bash
.venv\Scripts\pythonw.exe run_monitor.pyw
```

想從桌面開，就建立一個捷徑：目標是 `<專案>\.venv\Scripts\pythonw.exe`，引數是 `run_monitor.pyw`，開始位置設成專案資料夾。

只能開一個；重複開啟會提示「已經在執行」。不想看真實資料、只想看畫面時，加上 `--demo`。

## 畫面

**一般模式（預設）**：每個 agent 一張卡片。

- **今日 tokens**：本機時間 00:00 起的總用量，含快取讀取。括號內是不含快取讀取的量。
- **5 小時視窗內**：目前這個官方 5 小時額度視窗開始到現在的用量。沒有官方 5 小時視窗時，改算最近 5 小時。
- **進度條**：長度代表**剩餘**額度。
  - 綠色：剩 50% 以上
  - 琥珀色：剩 20–50%
  - 紅色：剩 20% 以下
  - 灰色：資料超過 30 分鐘沒更新
- **右側**：重置倒數。
- **最下面**：資料來源與時間。
  - 「即時查詢」是剛向官方查到的數字。
  - 「本機紀錄」「桌面 App 快照」是查詢失敗時的後備資料。
- **底部按鈕**：「立即更新」「懸浮模式」，以及「一般模式也置頂」。

**懸浮模式**：無邊框的小黑條，永遠置頂，每個 agent 一行，百分比都是**剩餘**額度。

- 左鍵拖曳：移動位置。
- 雙擊：回到一般模式。
- 右鍵：選單，可回一般模式、立即更新、調整透明度、結束。
- 滑鼠停在某一行 1 秒：顯示完整資訊。

位置、模式、透明度會記在 `ui_settings.json`，下次開啟時還原。

## 資料從哪裡來

| | 已用 token（每 30 秒讀本機紀錄） | 剩餘額度（每 3 分鐘查官方） |
|---|---|---|
| Claude Code | `%USERPROFILE%\.claude\projects\**\*.jsonl` | `api.anthropic.com/api/oauth/usage`。憑證是 Claude 桌面 App 的登入 token：讀 `config.json` 後用 Windows DPAPI 與 AES-GCM 解密。 |
| Codex | `%USERPROFILE%\.codex\sessions\**\rollout-*.jsonl` | `chatgpt.com/backend-api/wham/usage`。憑證是 `%USERPROFILE%\.codex\auth.json`。 |
| Antigravity | `%USERPROFILE%\.gemini\antigravity-cli\conversations\*.db` | `cloudcode-pa.googleapis.com` 的額度查詢。憑證在 Windows 認證管理員的 `gemini:antigravity`。 |

- 三家都**只公布剩餘百分比和重置時間，不公布 token 上限**，所以剩餘額度一律以 % 顯示。
- 2026-11-03 實測：
  - Codex 的即時查詢結果和本機紀錄完全一致（5 小時已用 18%、每週 30%）。
  - Antigravity 從對話資料庫算出的 token 數，和 agy 自己回報的 usage 完全一致。

**查詢失敗時的後備**：

- **Claude**：改顯示 Claude 桌面 App 記錄的用量快照（`plan-usage-history.json`，App 啟動時才更新）。
- **Codex**：改顯示本機紀錄裡最後一次的額度（Codex 執行時才更新）。
- **Antigravity**：沒有後備，會顯示原因，例如「需要重新登入 agy」。

## 安全性

這個工具會讀你本機的登入憑證去查官方額度，所以做了以下限制：

- **憑證只存在記憶體裡**：不顯示、不寫檔、不寫日誌、不放進錯誤訊息。
- **網路請求只送到白名單主機**：只用 HTTPS、不跟隨轉址、不經過系統代理。白名單：
  - `api.anthropic.com`
  - `chatgpt.com`
  - `daily-cloudcode-pa.googleapis.com`
  - `cloudcode-pa.googleapis.com`
  - `oauth2.googleapis.com`
- **只查詢，不消耗額度**：
  - 不呼叫任何推論 API。
  - 不執行 claude、codex、agy。
  - 不把刷新後的 token 寫回任何地方。
- **Antigravity 的 access token 過期時**：程式會從 agy 執行檔取出 OAuth client 設定，在記憶體中換一個新 token。
- 本機資料一律唯讀開啟。

## 設定（可選）

在專案根目錄建立 `config.json`，只寫要改的欄位：

```json
{
  "local_interval_s": 30,
  "remote_interval_s": 180,
  "enabled": ["claude", "codex", "agy"]
}
```

其他可改的欄位：

- `claude_home`、`codex_home`、`agy_home`：改資料夾位置。
- `agy_bin`：改 agy 執行檔路徑。
- `claude_desktop_dirs`：改桌面 App 資料夾。

查官方額度的間隔不要設太短，以免被限流（HTTP 429）。遇到限流時，程式會自動等待後再查。

## 疑難排解

數字看起來不對、或卡片出現警告時，跑診斷工具。它會印出三家的完整數字與錯誤原因，不會印出任何憑證：

```bash
.venv\Scripts\python.exe tools\diagnose.py
```

| 訊息 | 意思 |
|---|---|
| 即時查詢失敗（HTTP 401） | 登入過期。開一次 Claude 桌面 App、Codex 或 agy，讓它自己刷新登入。 |
| 需要重新登入 agy | Antigravity 的 refresh token 失效。在終端機執行 `agy` 重新登入。 |
| 5 小時 已重置，等待新資料 | 視窗剛重置，下一次查詢就會更新。 |

## 已知限制

- **Antigravity**：
  - 目前只看得到每週額度：官方只回傳 Gemini 和 Claude/GPT 兩個每週視窗，沒有 5 小時視窗。
  - token 用量只統計 agy CLI 的對話，不含 Antigravity IDE。
  - 一段對話的 token 全部算在它最後一次活動的時間點。
- **Claude**：官方回應沒有方案名稱，所以卡片不顯示方案。
- **Claude 的 token 數**：包含 subagent 和 side session，因為它們也寫在 `projects` 底下。數字以快取讀取為大宗，所以另外列出「不含快取讀取」的量。
- **開機自動啟動**：目前沒有設定。需要的話，把桌面捷徑複製到 `shell:startup` 資料夾即可。

## 檔案結構

```
monitor/
  model.py            共用資料結構
  collector.py        整合三家、管理查詢間隔與退避
  gui.py              視窗（一般／懸浮模式）
  sources/            各家的本機用量與官方額度
  secure_http.py      白名單 HTTPS
run_monitor.pyw       入口
tools/diagnose.py     診斷工具
tests/                pytest（140 個，全部離線、只用假資料）
specs/SPEC.md         規格
```

## 怎麼做出來的

由 Claude Code 主控，派工給其他 agent 完成：

1. **Antigravity（agy）**：調查 GitHub 類似專案。
2. **Codex**：閱讀 CodeZeno 原始碼，整理三家的額度查詢方式。
3. **Claude**：寫規格（`specs/SPEC.md`）與共用型別。
4. 同時派工：
   - Codex：寫後端，16 分鐘。
   - Codex：寫 GUI，8.5 分鐘。
   - agy：依規格寫黑箱測試，10 分鐘。
5. **Claude**：審查程式碼，用真實資料驗證三家數字、檢查輸出沒有憑證，微調畫面。

## 授權

本專案採 MIT 授權，全文見 `LICENSE`。

額度查詢與憑證解密的做法參考 CodeZeno/Claude-Code-Usage-Monitor（MIT），詳見 `THIRD_PARTY_NOTICES.md`。
