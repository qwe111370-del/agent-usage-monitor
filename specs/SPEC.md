# Agent 用量監控：規格

Windows 11 桌面小工具，顯示三個 agent 已經用了多少 token、官方額度還剩多少：

- Claude Code（`claude`）
- Codex（`codex`）
- Antigravity（`agy`）

- 語言：Python 3.12，只用標準函式庫加 `cryptography`。
- GUI：tkinter。
- 執行環境：專案的 `.venv`（已經裝好 cryptography、pytest）。
- 專案根目錄：本 repo 的根目錄（以下簡稱「專案」）。
- 程式註解、使用者看得到的文字一律用繁體中文。

## 0. 安全規則（所有人都要遵守）

1. **憑證只放在記憶體裡。** 包括 access token、refresh token、解密金鑰、OAuth client secret：
   - 不 print、不寫檔、不寫進日誌。
   - 不放進例外訊息或 `AgentStatus` 的任何欄位。
   - 例外訊息只能寫「HTTP 401」「找不到憑證」這類描述。
2. **網路請求只能送到白名單主機，而且只用 HTTPS。** 白名單：
   - `api.anthropic.com`
   - `chatgpt.com`
   - `daily-cloudcode-pa.googleapis.com`
   - `cloudcode-pa.googleapis.com`
   - `oauth2.googleapis.com`

   不跟隨任何轉址，timeout 15 秒。
3. **只做查詢，不做會消耗額度的動作。** 禁止：
   - 呼叫推論 API（例如 Anthropic `/v1/messages`）。
   - 執行 `claude`、`codex`、`agy` 指令。
   - 把刷新後的 token 寫回任何地方。
4. **本機資料一律唯讀開啟。** SQLite 用 `file:...?mode=ro` URI。
5. 測試只用自己建立的假資料（fixtures），**不讀真實的使用者目錄、不連網**。真實環境驗證由 Claude 負責。

## 1. 檔案與負責人

| 檔案 | 負責 | 說明 |
|---|---|---|
| `monitor/model.py` | 已完成，不要修改 | 共用資料結構：`TokenCounts`、`UsageEvent`、`QuotaWindow`、`AgentStatus` |
| `monitor/fmt.py` | 後端 | 數字與時間格式化 |
| `monitor/config.py` | 後端 | 設定檔 |
| `monitor/secure_http.py` | 後端 | 白名單 HTTPS 用戶端 |
| `monitor/aggregate.py` | 後端 | 區間加總 |
| `monitor/sources/claude_local.py`、`claude_quota.py` | 後端 | Claude |
| `monitor/sources/codex_local.py`、`codex_quota.py` | 後端 | Codex |
| `monitor/sources/agy_local.py`、`agy_quota.py` | 後端 | Antigravity |
| `monitor/sources/win_crypto.py` | 後端 | DPAPI、認證管理員（ctypes） |
| `monitor/collector.py` | 後端 | 整合成 `list[AgentStatus]` |
| `monitor/gui.py`、`monitor/ui_settings.py`、`monitor/demo_data.py`、`run_monitor.pyw` | GUI | 視窗 |
| `tests/` | 測試 | pytest |

每個人只建立和修改自己負責的檔案。

## 2. 介面（後端必須照這個簽名實作，GUI 和測試都依賴它）

### monitor/fmt.py

```python
def format_tokens(n: int) -> str
```

- 小於 1 萬：千分位，例如 `"9,876"`。
- 1 萬到 1 億以下：以「萬」為單位，取整數、加千分位，例如 26_396_605 → `"2,640 萬"`、15_000 → `"2 萬"`（四捨五入）。
- 1 億以上：以「億」為單位，小數一位，例如 123_456_789 → `"1.2 億"`。
- 0 → `"0"`；負數視為 0。

```python
def format_countdown(seconds: float) -> str
```

- 小於等於 0 → `"已重置"`。
- 小於 1 小時 → `"45 分"`（無條件進位到分，最少 `"1 分"`）。
- 小於 1 天 → `"2 小時 14 分"`；剛好整點 → `"3 小時"`。
- 1 天以上 → `"3 天 4 小時"`；剛好整天 → `"2 天"`。

```python
def format_clock(ts: float) -> str
```

- 本機時間。今天 → `"21:15"`；不是今天 → `"11/02 21:15"`。

### monitor/aggregate.py

```python
def sum_between(events: Iterable[UsageEvent], start: float, end: float) -> TokenCounts
```

- 加總 `start <= ts < end` 的事件。

```python
def local_midnight(now: float) -> float
```

- `now` 這一天本機時間 00:00 的 epoch 秒。

### monitor/config.py

```python
@dataclass
class Config:
    local_interval_s: int = 30        # 讀本機紀錄的間隔
    remote_interval_s: int = 180      # 查官方額度的間隔
    enabled: tuple[str, ...] = ("claude", "codex", "agy")
    claude_home: Path | None = None   # None = 環境變數 CLAUDE_CONFIG_DIR，否則 ~/.claude
    codex_home: Path | None = None    # None = 環境變數 CODEX_HOME，否則 ~/.codex
    agy_home: Path | None = None      # None = ~/.gemini/antigravity-cli
    agy_bin: Path | None = None       # None = 自動尋找（見 5.3）
    claude_desktop_dirs: list[Path] | None = None  # None = 自動尋找（見 3.2）

def load_config(path: Path | None = None) -> Config
```

- 預設讀專案的 `config.json`。檔案不存在就回傳預設值；未知欄位忽略；路徑欄位是字串。

### monitor/secure_http.py

```python
class HttpError(Exception):
    status: int | None        # HTTP 狀態碼；網路錯誤為 None
    retry_after: float | None # 429 時解析 Retry-After（秒）

def get_json(url: str, headers: dict[str, str]) -> Any
def post_json(url: str, headers: dict[str, str], body: dict) -> Any
def post_form(url: str, form: dict[str, str]) -> Any
```

- 主機不在白名單、或不是 https，丟 `ValueError`。
- 用 urllib，自訂 opener 禁止轉址：遇到 3xx 直接丟 `HttpError`。
- 非 2xx 丟 `HttpError(status)`，訊息不得包含 header 或 body 內容。

### monitor/collector.py

```python
class Collector:
    def __init__(self, config: Config | None = None, now: Callable[[], float] = time.time): ...
    def collect(self, force_remote: bool = False) -> list[AgentStatus]
```

- 只會被一個背景執行緒呼叫，所以不需要鎖。
- 依 `AGENT_ORDER` 回傳啟用中的 agent，每個 agent 一定回傳一個 `AgentStatus`。
- 單一 agent 出錯時：把錯誤寫進該 agent 的 `error`，其他 agent 照常回傳。整個函式不得丟例外。
- 本機紀錄每次都增量讀取。
- 官方額度依 agent 各自計時：距上次查詢滿 `remote_interval_s`，或 `force_remote=True`，才重新查。沒到時間就沿用上次結果。
- 遇到 429：等 `retry_after` 秒；沒有這個值就等 2 倍間隔，最多 15 分鐘。
- `tokens_window` 與 `window_label`：
  - 取第一個視窗長度約 5 小時的額度。視窗起點 = `resets_at - 5 小時`，用量 = 起點到現在。
  - 沒有官方視窗時，用「最近 5 小時」，`window_label="近 5 小時"`。
- 額度已過期：任一 `QuotaWindow` 若 `resets_at <= now`，`used_pct` 改成 0，並加一條 note：「{label} 已重置，等待新資料」。

## 3. Claude Code

### 3.1 token 用量：`sources/claude_local.py`

- 檔案：`<claude_home>/projects/**/*.jsonl`，包含子目錄裡的 subagent 檔案。
- 只處理最後修改時間在 8 天內的檔案。

每一行是一個 JSON。符合以下全部條件才算一筆：

- `type == "assistant"`。
- 有 `message.usage`。
- 有 `timestamp`（ISO 8601，結尾可能是 `Z`）。

換算方式（usage 欄位缺少時視為 0）：

| TokenCounts 欄位 | usage 欄位 |
|---|---|
| `input` | `input_tokens` |
| `output` | `output_tokens` |
| `cache_write` | `cache_creation_input_tokens` |
| `cache_read` | `cache_read_input_tokens` |

去重規則：

- 去重鍵是 `(message.id, requestId)`；同一個鍵只算第一次。同一則回應會在多行重複出現，跨檔案也要去重。
- 兩個值都沒有時，不去重。

```python
def parse_line(line: str) -> tuple[tuple | None, UsageEvent] | None
```

- 純函式：回傳 `(去重鍵或 None, 事件)`；不符合條件、或 JSON 壞掉，回傳 None。

```python
class ClaudeLogReader:
    def __init__(self, projects_dir: Path, keep_days: float = 8): ...
    def refresh(self, now: float | None = None) -> None
    @property
    def events(self) -> list[UsageEvent]
```

- **增量讀取**：每個檔案記住 `(大小, mtime, 已讀位移, 沒讀完的半行)`。
  - 檔案變大：只讀新增的位元組。最後一行沒有換行時，先保留起來等下次。
  - 檔案變小或被換掉：整檔重讀，並移除該檔的舊事件。
- `events` 只保留 `keep_days` 天內的事件，依 ts 排序。
- 用 `open(..., "rb")` 讀取，再以 UTF-8 解碼，錯誤用 `replace`。

### 3.2 官方額度：`sources/claude_quota.py`

**取得 access token**。依序嘗試，第一個成功的就用：

1. **Claude 桌面 App**
   - 資料目錄依序搜尋：
     - `%LOCALAPPDATA%\Claude-Data\claude_<13 個小寫英數字>\`，優先 `claude_pzs8sxrjxfjjc`
     - `%APPDATA%\Claude\`
     - `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\`
   - 每個資料目錄：
     1. 讀 `config.json`，依序取頂層字串欄位 `oauth:tokenCacheV2`、`oauth:tokenCache`，略過空值。
     2. 讀同目錄的 `Local State`（JSON），取 `os_crypt.encrypted_key`。Base64 解碼後去掉開頭 ASCII `DPAPI`，再呼叫 `win_crypto.dpapi_unprotect()` 得到 AES 金鑰。
     3. token cache 字串 Base64 解碼後必須以 ASCII `v10` 開頭。去掉 `v10` 後，前 12 bytes 是 nonce，其餘是「密文＋最後 16 bytes 的 tag」。用 `cryptography` 的 `AESGCM(key).decrypt(nonce, 密文含tag, None)` 解密。
     4. 明文是 JSON object：鍵是 `<install>:<user>:<base url>:<scopes>`，值是 `{"token": str, "expiresAt": 毫秒}`。
   - 挑 token 的優先順序：
     1. 鍵同時包含 `user:inference` 和 `user:profile`。
     2. 只包含 `user:inference`。
     3. 其他。
   - 同一優先級挑 `expiresAt` 最大的；只考慮 token 非空白、而且 `expiresAt` 晚於現在的。
2. **CLI 憑證檔**：`<claude_home>/.credentials.json` 的 `claudeAiOauth.accessToken`、`claudeAiOauth.expiresAt`（毫秒）。已過期就跳過。

可單獨測試的純函式：

```python
def decrypt_v10(key: bytes, b64_value: str) -> bytes
```

- 格式錯誤丟 `ValueError`。

```python
def pick_token(entries: dict, now_ms: float) -> str | None
```

**查詢額度**：

```http
GET https://api.anthropic.com/api/oauth/usage
Authorization: Bearer <token>
anthropic-beta: oauth-2025-04-20
User-Agent: agent-usage-monitor/1.0
```

```python
def parse_usage_response(obj: dict) -> list[QuotaWindow]
```

- `five_hour`：`{"utilization": 已用百分比 0–100（已經是百分比，不要再乘 100）, "resets_at": ISO 8601}`。
  - 轉成 `QuotaWindow("five_hour", "5 小時", utilization, resets_at, 18000)`。
- `seven_day`：同上。轉成 `QuotaWindow("seven_day", "每週", ...)`，`window_seconds=604800`。
- `seven_day_opus`、`seven_day_sonnet`：如果是 object 而且有數字 `utilization`，額外加入，label 分別是「每週 Opus」「每週 Sonnet」。
- 欄位缺少或為 null 就略過該視窗。
- 若 `five_hour`／`seven_day` 缺少，但有 `limits` 陣列：
  - 從中找沒有 `scope` 的元素補上：`kind == "session"` → 5 小時，`kind == "weekly_all"` → 每週。
  - 百分比取 `percent`，沒有時取 `utilization`；`resets_at` 是 ISO 8601。
- 順序：5 小時、每週，再來是其他。

**沒有可用 token、或查詢失敗時的後備：桌面 App 的用量快照**

- 檔案：上面找到的桌面資料目錄裡的 `plan-usage-history.json`。

```python
def parse_plan_usage_history(obj: dict) -> tuple[float, list[QuotaWindow]] | None
```

- 格式：`{"version":2, "samples":[{"t": 毫秒, "org": "...", "u": {"fh": 5 小時已用 %, "sd": 每週已用 %}}]}`。
- 取 `t` 最大、而且 `u` 不是空的那筆，回傳 `(t/1000, [5 小時, 每週])`；`resets_at` 為 None。沒有就回傳 None。
- 這時 `quota_source="snapshot"`，note 寫：「即時查詢失敗（原因），顯示桌面 App 在 {時間} 記錄的快照」。

`plan`：Claude 的回應沒有方案名稱，設為 None。

## 4. Codex

### 4.1 token 用量與本機額度：`sources/codex_local.py`

- 檔案：`<codex_home>/sessions/**/rollout-*.jsonl` 與 `<codex_home>/archived_sessions/**/rollout-*.jsonl`；只處理 8 天內修改過的。
- 只看 `{"type":"event_msg","payload":{"type":"token_count", ...}}` 這種行。`timestamp` 是 ISO 8601。

**用量**：

- 讀 `payload.info.total_token_usage`，它是**該檔案的累計值**，欄位有：
  - `input_tokens`，**已包含** `cached_input_tokens`
  - `cached_input_tokens`
  - `output_tokens`，已包含 reasoning
- 每個事件的用量 = 本次累計 − 同檔上次累計；同檔第一筆就直接用累計值。
- 差值換算：
  - `input = Δinput_tokens − Δcached_input_tokens`
  - `cache_read = Δcached_input_tokens`
  - `output = Δoutput_tokens`
  - `cache_write = 0`
- 負數差值視為 0，代表累計被重設。
- `info` 為 null 的事件不算用量，但仍可能帶 `rate_limits`。

**本機額度**：

- 所有檔案中 timestamp 最新、而且有 `payload.rate_limits` 的那筆。

```python
def rate_limits_to_windows(rl: dict) -> tuple[str | None, list[QuotaWindow]]
```

- 輸入格式：`{"primary": {"used_percent": 51.0, "window_minutes": 300, "resets_at": epoch秒}, "secondary": {...}, "plan_type": "plus"}`。
- 回傳 `(plan_type, windows)`。
- 視窗名稱看 `window_minutes`：
  - 300 → key `"five_hour"`、「5 小時」
  - 10080 → `"seven_day"`、「每週」
  - 其他 → `f"{分鐘}m"`，label 依長度寫成「N 小時」或「N 天」
- `primary`／`secondary` 為 null 就略過。

```python
class CodexLogReader:
    def __init__(self, roots: list[Path], keep_days: float = 8): ...
    def refresh(self, now: float | None = None) -> None
    @property
    def events(self) -> list[UsageEvent]
    @property
    def latest_rate_limits(self) -> tuple[float, dict] | None  # (時間, rate_limits)
```

增量讀取規則同 3.1。

### 4.2 即時額度：`sources/codex_quota.py`

- 憑證：`<codex_home>/auth.json` 的 `tokens.access_token` 與 `tokens.account_id`（可省略）。

```http
GET https://chatgpt.com/backend-api/wham/usage
Authorization: Bearer <access_token>
User-Agent: codex-cli
ChatGPT-Account-Id: <account_id>   ← 只有 account_id 非空才加
```

```python
def parse_usage_response(obj: dict) -> tuple[str | None, list[QuotaWindow]]
```

- 來源欄位：
  - `rate_limit.primary_window` 與 `rate_limit.secondary_window`，各自有 `used_percent`、`reset_at`（epoch 秒）、`limit_window_seconds`。
  - 頂層 `plan_type`（可能沒有）。
- 視窗分類：
  - `limit_window_seconds >= 86400` → 每週。
  - 其他 → 5 小時；label 依秒數換算。
  - 沒有 `limit_window_seconds` 時：primary = 5 小時、secondary = 每週。
- 查詢失敗時（沒有憑證、401、403、網路錯誤）：
  - 改用 4.1 的本機額度，`quota_source="local"`，`quota_as_of` = 那筆紀錄的時間。
  - note：「即時查詢失敗（原因），改用本機紀錄」。
  - **不要**執行 codex 來刷新 token。

## 5. Antigravity（agy）

### 5.1 token 用量：`sources/agy_local.py`

- 檔案：`<agy_home>/conversations/*.db`（SQLite，唯讀）。
- 只在 db、`-wal` 檔的大小或 mtime 改變時重讀該檔。
- 讀取失敗（例如被鎖住）時，沿用上次結果，下次再試。

**用量**：

- 來源：`gen_metadata` 資料表 `data` 欄位（protobuf，沒有 schema，用通用 varint 解碼）。
- 路徑「欄位 1（訊息）→ 欄位 4（訊息）」底下的 varint：

| 欄位 | 意義 |
|---|---|
| 2 | input（未快取） |
| 3 | output |
| 5 | cache_read |
| 9 | thinking |

- 換算：
  - `input = 2`
  - `output = 3 + 9`
  - `cache_read = 5`
  - `cache_write = 0`
- 實測一個對話 44 筆加總：欄位 2 = 199,976、欄位 3 = 12,579、欄位 9 = 8,054、欄位 5 = 1,232,695，與 agy 自己回報的 usage 完全一致。

**時間**：

- 來源：`steps` 資料表 `metadata` 欄位，「欄位 1 → 欄位 1」的 varint 是 epoch 秒。
- 整個對話的用量合成**一個** `UsageEvent`，時間取所有 step 時間的最大值；都沒有時用 db 的 mtime。

```python
def decode_usage(blob: bytes) -> TokenCounts | None
def decode_step_time(blob: bytes) -> float | None
```

- 解不出來回傳 None，**不得丟例外**。protobuf 格式壞掉、長度超出等情況都要處理。
- 解碼時：
  - 要支援 wire type 0、1、2、5。
  - 欄位 1、4 若重複出現，取第一個。

```python
class AgyConversationReader:
    def __init__(self, conversations_dir: Path): ...
    def refresh(self) -> None
    @property
    def events(self) -> list[UsageEvent]
```

### 5.2 憑證：`sources/win_crypto.py`

```python
def dpapi_unprotect(data: bytes) -> bytes
```

- 用 ctypes 呼叫 `crypt32.CryptUnprotectData`（entropy、prompt 傳 NULL，flags 0），並用 `LocalFree` 釋放輸出。

```python
def read_generic_credential(target: str) -> bytes | None
```

- 用 ctypes 呼叫 `advapi32.CredReadW(target, 1, 0, byref(ptr))`，複製 `CredentialBlob` 後 `CredFree`。找不到回傳 None。

### 5.3 官方額度：`sources/agy_quota.py`

**憑證**：

- Windows 認證管理員，target 是 `gemini:antigravity`。
- blob 是 UTF-8 JSON；若長度為偶數、而且每個奇數位置的 byte 都是 0，改用 UTF-16LE 解碼。
- 格式：`{"token": {"access_token": str, "refresh_token": str, "expiry": ISO 8601}}`。

**刷新**：

- 觸發條件：access_token 空白，或 expiry ≤ 現在 + 60 秒，而且有 refresh_token。
- 請求：

  ```http
  POST https://oauth2.googleapis.com/token
  ```

  form 欄位：
  - `client_id`
  - `client_secret`
  - `refresh_token`
  - `grant_type=refresh_token`
- 回應只取 `access_token` 與 `expires_in`，**只存在記憶體**，到期前重複使用。
- 查詢用量時遇到 401／403 而且本輪還沒刷新過：刷新一次再重試。
- `client_id`／`client_secret` 從 agy 或 Antigravity IDE 的執行檔中抽取。
  - 執行檔依序找，第一個存在、而且抽得到的就用：
    - `config.agy_bin`
    - 環境變數 `AGY_BIN`
    - `%LOCALAPPDATA%\agy\bin\agy.exe`
    - `%LOCALAPPDATA%\Programs\Antigravity\resources\bin\language_server.exe`
    - `%LOCALAPPDATA%\Programs\Antigravity IDE\resources\bin\language_server.exe`
  - 抽出來的值只快取在記憶體。
  - 抽取在背景執行緒進行（檔案很大）；可用 mmap，或分塊讀取、塊與塊之間保留重疊。

```python
def extract_oauth_clients(data: bytes) -> list[tuple[str, str]]
```

- client_id：regex `rb"\d{10,}-[a-z0-9]{20,80}\.apps\.googleusercontent\.com"`。
- client_secret：regex `rb"GOCSPX-[A-Za-z0-9_\-]{28}"`。
- 各自去重後，回傳所有 (id, secret) 配對，順序依出現位置。刷新時逐一嘗試。
- 遇到 `invalid_client`、`unauthorized_client` 就換下一組；`invalid_grant` 代表要重新登入，直接停止。

**查詢**：

- base URL 依序嘗試：
  - `https://daily-cloudcode-pa.googleapis.com`
  - `https://cloudcode-pa.googleapis.com`
- header：
  - `Authorization: Bearer <token>`
  - `Content-Type: application/json`
  - `User-Agent: antigravity/cli/1.4.0`
- 呼叫順序：
  1. `POST /v1internal:loadCodeAssist`，body `{"metadata":{"ideType":"ANTIGRAVITY"}}`，取回應的 `cloudaicompanionProject`（字串，可能為空）。
  2. 有 project 時：`POST /v1internal:retrieveUserQuotaSummary`，body `{"project": project}`。
  3. 步驟 2 失敗或沒有 project 時：`POST /v1internal:fetchAvailableModels`，body `{"project": project}`；沒有 project 就送 `{}`。

```python
def parse_quota_summary(obj: dict) -> list[QuotaWindow]
```

- 輸入格式：`{"groups":[{"displayName","description","buckets":[{"bucketId","displayName","window","remainingFraction","resetTime"}]}]}`。
- 每個 bucket 產生一個視窗：
  - `used_pct = (1 − clamp(remainingFraction, 0, 1)) × 100`
  - `resets_at` = `resetTime`（ISO 8601）
  - `window`：`"5h"`（不分大小寫）→ 18000 秒、label 後綴「5 小時」；`"weekly"` → 604800 秒、「每週」；其他 → 原字串
  - label = `f"{group displayName 或 bucket displayName} {後綴}"`
  - key = `f"{bucketId}:{window}"`
- 排序：
  1. 名稱含 gemini 的 group 排最前面。
  2. 同一 group 內，5 小時排在每週前面。

```python
def parse_available_models(obj: dict) -> list[QuotaWindow]
```

- 輸入格式：`{"models": {"<model id>": {"quotaInfo": {"remainingFraction": 0.8, "resetTime": "..."}}}}`。
- 每個有 `quotaInfo` 的模型產生一個視窗：key 是模型 id，label 是模型 id，`window_seconds=None`。
- 依剩餘比例由少到多排序，最多回傳 6 個。

- `plan`：None。
- 查詢失敗時，`quota_source="none"`；note 寫明原因，例如「找不到 Antigravity 登入憑證」「需要重新登入 agy」「HTTP 403」。

## 6. GUI：`monitor/gui.py`

### 6.1 共同

- 入口：`run_monitor.pyw`。
  - 以 `pythonw` 執行，不開主控台。
  - 加上 `--demo` 參數時，改用 `monitor/demo_data.py` 的假資料，方便沒有真實資料時開發與截圖。
- 只能開一個：用 `kernel32.CreateMutexW("Local\\AgentUsageMonitor")`。已存在時，用 messagebox 提示「已經在執行」後結束。
- 啟動時呼叫 `ctypes.windll.shcore.SetProcessDpiAwareness(1)`，失敗就忽略。
- 字型：`Microsoft JhengHei UI`。
- **執行緒**：
  - 背景 `threading.Thread(daemon=True)` 每 `config.local_interval_s` 秒呼叫一次 `collector.collect()`，結果放進 `queue.Queue`。
  - 主執行緒用 `root.after(500, ...)` 取出結果再更新畫面。
  - **tkinter 只能在主執行緒操作。**
  - 「立即更新」：用 `threading.Event` 通知背景執行緒立刻以 `force_remote=True` 執行一次。
- 倒數文字每 30 秒在主執行緒重算一次（不必重新收集）。
- 顏色依剩餘 %：
  - ≥ 50：綠 `#2e9e5b`
  - 20–50：琥珀 `#d99a1e`
  - < 20：紅 `#d64545`
  - 資料是 snapshot 或 local，而且超過 30 分鐘：灰 `#8a8a8a`
- 畫面設定存在專案的 `ui_settings.json`，由 `monitor/ui_settings.py` 負責讀寫：
  - 模式（normal／compact）
  - 兩種模式各自的視窗位置
  - 透明度
  - 檔案壞掉時用預設值。

### 6.2 一般模式（預設）

- 一般視窗，標題「Agent 用量監控」，可縮到工作列。
- 每個 agent 一張卡片，由上到下：

```
Claude Code                                    （有 plan 時在右邊顯示，例如「Plus 方案」）
今日 2,640 萬 tokens（不含快取讀取 120 萬）｜5 小時視窗內 1,898 萬
5 小時  [██████████░░░░░] 剩 72%   2 小時 14 分後重置
每週    [█████████████░░] 剩 90%   3 天 4 小時後重置
即時查詢・21:15 更新        ← quota_source / quota_as_of
⚠ note 或 error（有的話，一行一則，小字）
```

- 進度條用 Canvas 畫，長度代表**剩餘** %，顏色規則見 6.1。
- `resets_at` 為 None 時不顯示倒數。
- 來源文字：
  - live → 「即時查詢・{時間} 更新」
  - local → 「本機紀錄・{時間}（Codex 執行時才會更新）」
  - snapshot → 「桌面 App 快照・{時間}」
  - none → 「沒有額度資料」
- 視窗太多（Antigravity 可能有 4～6 個）時，全部列出，卡片自動長高。
- 底部列：
  - 「最後更新 21:15:03」
  - 按鈕：「立即更新」「懸浮模式」
  - 核取方塊：「一般模式也置頂」

### 6.3 懸浮模式

- 無邊框（`overrideredirect(True)`）、永遠置頂（`-topmost`）、透明度 0.92，深色背景 `#1e1e1e`、淺色字。
- 每個 agent 一行，大約像這樣：

  ```
  Claude  5h 72% · 週 90% · 今日 2,640萬
  Codex   5h 100% · 週 72% · 今日 71萬
  Antigr  Gemini 5h 80% · 今日 21萬
  ```

  - 百分比是**剩餘**，依顏色規則上色。
  - 每個 agent 最多顯示前 2 個視窗。
- 操作：
  - 滑鼠左鍵拖曳可移動。
  - 雙擊：回到一般模式。
  - 右鍵選單：一般模式、立即更新、透明度（70%／85%／92%／100%）、結束。
  - 滑鼠停在某一行上 1 秒：用 tooltip 顯示該 agent 的完整資訊（各視窗、倒數、來源、note）。
- 切換模式時，記住並還原兩種模式各自的位置。

### 6.4 `monitor/demo_data.py`

```python
def demo_statuses(now: float) -> list[AgentStatus]
```

- 產生三家的假資料，要涵蓋：
  - live、local、snapshot 三種來源
  - 剩餘 85%、35%、10% 三種顏色
  - 一則 note、一則 error
  - Antigravity 有 4 個視窗

## 7. 不在範圍內

- 開機自動啟動（之後問使用者）。
- 系統匣圖示。
- 費用估算。
- 任何把資料送到白名單以外的功能。
