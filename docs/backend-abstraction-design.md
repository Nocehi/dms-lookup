# 字典查詢後端抽象與加速方案（設計草稿）

狀態：方案 A（每個來源一個 sdcv stdin 會話，`SdcvSession`）已實作並隨 PR #1 合併（2026-11-02）；方案 B、C、D 仍未實作。以下為原設計稿，保留作脈絡。

## 1. 結論摘要

1. **sdcv 0.5.5 的 stdin 互動模式本身就支援一個程序連續處理任意多個查詢**（`src/sdcv.cpp:240`：`while (io->read("Enter word or phrase: ", phrase)) lib.process_phrase(...)`）。現行 `run_bounded_protocol` 只是在第二個提示出現時主動關閉 stdin，並非 sdcv 的限制。`-n`（非互動）模式則**完全不讀 stdin**（`sdcv.cpp:248-250`，沒有 argv 詞表就直接印 "There are no words/phrases to translate"），因此「逐行多查詢」只能走互動模式，而互動模式正是現行協議用的模式。
2. 建議採 **方案 A：單程序批次會話（session）**：每次 lookup 對每個來源（或每個 data_dir 群組）只開一個 sdcv 程序，exact、同形詞 choice、`/fuzzy`、候選回查全部在同一程序內以多個提示框（frame）完成。索引只載入一次，程序數由「Linux」的 21 個降到 4 個（v0.7.1 水平）或更低；核心改動集中在 `SdcvClient.query()` 與 stdin 模組，`_lookup_modes()` 與 fixture 語義不變。
3. 常駐 sdcv（方案 B）和純 Python StarDict（方案 C）都應放在同一個 `DictionaryBackend` 介面之後，作為日後可選實作；兩者在私隱、記憶體或 fixture 相容上各有未解的代價，不建議作為本輪的第一步。
4. Related 延遲載入（方案 D）與 A 互補，但在 A 落地後收益有限（fuzzy 在同一程序內只多幾毫秒），建議先量測再決定。

## 2. 現況與回歸根因

### 2.1 呼叫鏈（分支 `fix/post-rewrite-regressions`，c2cef49）

```
lookup()                                   lib/dank_lookup_core.py
  └─ _lookup_modes(query, config, client)   每個 mode × 每個 eligible source
       ├─ client.source_installed(source)   → sdcv --list-dicts（每個 data_dir 快取一次）
       └─ client.query(source, query, candidates=…)
            ├─ _exact_query(q)              1 個程序；若遇同形詞 prompt，每個 index 再 1 個程序
            ├─ _stdin_query("/"+q, choice=0) 1 個程序，只為取得候選清單（choice 0 的詞條被丟棄）
            └─ 每個候選（≤16）_exact_query  各 1 個程序（＋同形詞再各 1 個）
```

每個程序都經 `run_bounded_protocol()`（`lib/dank_lookup_sdcv_stdin.py`）：啟動 sdcv → 等第一個 `Enter word or phrase: ` → 送查詢 → （可選）等 `Your choice[-1 to abort]: ` → 送 choice → 等第二個主提示 → 關閉 stdin → 等程序結束。

### 2.2 代價

第 2 階段量測（`notes/phase2-2026-11-02.md` §4，使用者 config 副本，one-shot backend，5 次中位數）：

| 輸入 | v0.7.1 | HEAD 修正後 | sdcv 程序數 v0.7.1 → HEAD |
|---|---|---|---|
| Linux | 47 ms | 267 ms | 4 → 21 |
| 春 | 150 | 179 | 10 → 16 |
| 中文長句（8 token） | 1309 | 1203 | 90 → 121 |

「Linux」的 21 個程序：`--list-dicts` ×2、GCIDE exact 1 ＋ `/Linux` 1 ＋ 10 個候選回查、WordNet exact 1 ＋ `/Linux` 1 ＋ 候選回查（扣除 exact 已有者）。每個 sdcv 程序約 10–15 ms（fork/exec、glib 初始化、掃描 data_dir 內所有 `.ifo`、載入已啟用字典的索引），這個固定成本乘以程序數就是回歸的全部來源；sdcv 查詢本身（二分搜尋 + 讀一條 dict 資料）在 1 ms 以內。

v0.7.1 只需 1 個程序，是因為它用 `-n`（force）模式：`process_phrase(..., force=true)` 會把模糊結果全部印出，不經 choice prompt（`libwrapper.cpp:435-447`）。0.8.0 為了不把查詢放進 argv 改走 stdin，而 stdin 只有互動模式，互動模式遇到「同一字典多於一條結果」必定進 choice prompt、一次只印一條（`libwrapper.cpp:386-430`）。這是結構性的：**互動模式下沒有任何旗標能一次印出全部模糊結果**，所以每個候選都要回查，差別只在「是否每次回查都重開程序」。

## 3. sdcv 0.5.5 原始碼確認（供後續實作與 gate 引用）

來源：`Dushistov/sdcv` tag `v0.5.5`，`src/`。

| 事實 | 位置 |
|---|---|
| 互動主迴圈：每讀一行就 `process_phrase`，直到 stdin EOF；每個查詢輸出獨立的 `[...]\n` JSON 陣列，`first_result` 每次重置 | `sdcv.cpp:238-246`、`libwrapper.cpp:374-376, 454-456` |
| `-n` 不讀 stdin | `sdcv.cpp:227-237, 248-250` |
| JSON 模式下 choice prompt 仍會出現（`!force && !show_all_results`），選擇後 `break` 回主迴圈；`-1` 中止 | `libwrapper.cpp:401-430` |
| 同一字典 >1 條結果就不是 `show_all_results`（exact 同形詞也會觸發） | `libwrapper.cpp:380-397` |
| `/` 前綴＝模糊（`LookupWithFuzzy`，編輯距離 <3、每個 lib 最多 `MAXFUZZY=10` 個候選，再對每個候選做 `SimpleLookup`）；`|` 前綴＝資料搜尋；含未跳脫 `*`/`?` ＝ glob 規則搜尋 | `stardict_lib.cpp:1550-1584`、`libwrapper.cpp:216-228`、`stardict_lib.cpp:1347-1420` |
| `--exact-search` 只是關掉「exact 找不到時自動 fuzzy」（`fuzzy_=false`），不影響 `/`、`|`、`*?` 的路由 | `libwrapper.cpp:362-366`、`stardict_lib.cpp:1340-1345` |
| 索引：`.idx` 走 `OffsetIndex`（`.oft` 快取 mmap、每頁 32 條按需 `fread`），`.idx.gz` 走 `WordListIndex`（整個索引進記憶體）；dict 資料快取 10 條 | `stardict_lib.cpp:945-953, 420-700`、`stardict_lib.hpp:36` |
| 歷史：有 readline 時每個查詢及被選中的詞條都 `add_history()` 到**程序內記憶體**，程序結束時寫入 `SDCV_HISTFILE`，再按 `SDCV_HISTSIZE` 截斷 | `readline.cpp:66-108`、`libwrapper.cpp:330-331, 423` |
| `--use-dict` 以外的字典進 `disable_list`，不載入；但 data_dir 內所有 `.ifo` 仍會被掃描解析一次 | `sdcv.cpp:165-193`、`stardict_lib.cpp:1015-1023` |
| `--utf8-input/--utf8-output` 下不做 locale 轉換；`LC_ALL=C.UTF-8` 保證提示字串為英文（gettext 未命中） | `libwrapper.cpp:336-343` |

兩點現行程式未處理、任何方案都應一併定案：

- 使用者選取的文字若以 `/` 或 `|` 開頭、或含 `*`/`?`，sdcv 會改走模糊／資料／規則搜尋，而非 exact。`*`、`?` 可用 `\` 跳脫，`/`、`|` 開頭則無法在 exact 模式下查到。後端介面應在此層把這種輸入標為 `unsupported_query_syntax` 或自行跳脫，而不是交給 sdcv 解釋。
- 常駐程序的 readline 記憶體歷史會隨查詢累積（見 §5 方案 B）。

## 4. 後端抽象

### 4.1 現有的事實邊界

`_lookup_modes()` 對 client 只用到：`available`、`source_installed(source)`、`query(source, query, candidates=…)`、`backend_ns`、`budget_exhausted`、`deadline_ns`、`cancel_event`；`lookup()` 另外讀 `clock_ns`。`tests/test_core.py` 的 `FakeClient`／`TokenBudgetClient` 已經以這組介面替身，證明邊界存在，只是沒有命名。

### 4.2 建議的介面（`lib/dank_lookup_backend.py`，新檔）

```python
class DictionaryBackend(Protocol):
    available: bool
    backend_ns: int              # 累計後端耗時（lookup 的 timing 用）
    deadline_ns: int | None      # 整體預算；由 lookup() 設定
    budget_exhausted: bool
    cancel_event: threading.Event | None
    clock_ns: Callable[[], int]

    def source_installed(self, source: Mapping[str, Any]) -> bool | None: ...
    def query(
        self, source: Mapping[str, Any], query: str, *, candidates: str
    ) -> tuple[list[Entry], Warning | None]: ...
    def close(self) -> None: ...   # 釋放會話／常駐資源；one-shot 實作為 no-op
```

- `Entry` 維持 sdcv JSON 形狀 `{"dict","word","definition"}`，`_lookup_modes()` 之後的 `is_exact`、`render_entries`、entry_id 計算全部不動，fixture 不受影響。
- `candidates`（`all`／`variants`／`fallback`）與 `V1_FUZZY_FALLBACK_KEY` 仍是 core 的政策，後端只負責「exact ＋ 有界候選」的取回；這與 c2cef49 的分工一致。
- 警告碼維持現有字串（`sdcv_source_timeout`、`sdcv_stdout_cap`、`lookup_budget_exhausted` …），以免 `docs/json-schema.md` 與 QML 的對應改動。
- `lookup()` 的最小改動：型別註解改為 `DictionaryBackend`；在 `finally` 呼叫 `client.close()`；工廠函數 `make_backend(config)` 依 `config["backend"]`（預設 `"sdcv"`）選實作。`backend` 欄位在輸出 JSON 已存在（`"sdcv" | "unavailable"`），新增實作時照樣填入。

### 4.3 sdcv 側的分層

```
SdcvClient (DictionaryBackend)            政策：exact→同形詞→fuzzy→候選回查、cap、預算、去重
  └─ SdcvSession                          一個 sdcv 程序；方法 ask(query, choice=None) -> Frame
       └─ run_bounded_session()           lib/dank_lookup_sdcv_stdin.py：I/O 迴圈、提示偵測、cap、逾時、kill
```

`run_bounded_protocol()` 目前把「送一個查詢、等第二個提示、關閉」寫死在 I/O 迴圈裡。改成**由呼叫端驅動的 frame 迴圈**：迴圈只負責讀到下一個主提示或 choice 提示就把 frame 交回；呼叫端決定下一步送甚麼或關閉。現有的 `parse_protocol_stdout()` 以單 frame 為單位，可直接重用於每個 frame。原來的 `run_bounded_protocol()` 可以保留為 `SdcvSession` 的單查詢包裝，讓 `tests/probe_sdcv_stdin.py` 的 gate 與 `fixtures-sdcv-stdin` 不必重寫；新的多 frame 路徑另加 gate case（同程序連續兩個查詢、查詢→choice→查詢、空行、EOF）。

## 5. 方案比較

### 方案 A：單程序批次會話（建議）

每個 `query(source, q)` 開一個 `SdcvSession`，依序送：`q` →（若 choice prompt）每個 index 再送 `q` + choice → `/q` + choice 0 → 每個候選 `w`（＋同形詞）。全部 frame 在同一程序內；預算與 cancel 檢查在每個 frame 之間做。

進一步可把會話提升到 **每個 data_dir 一個程序**（`--use-dict` 傳入該 data_dir 下所有 eligible 來源）：JSON 每條都帶 `dict`，候選清單每行也帶字典名，現有 parser 已能按字典分流；choice prompt 在多字典時會把各字典的結果合併編號，`_exact_query` 的逐 index 回查邏輯照用。這一步把「中文長句」的程序數從 121 降到約 8（每個 token 一個），甚至整個 lookup 一個程序（所有 token 共用，因為字典集合是 config 決定的，與 token 無關）。建議分兩步落地：先每來源一個程序（語義與現狀一一對應，fixture 零風險），量測後再合併。

| 面向 | 評估 |
|---|---|
| 延遲 | 「Linux」估計 21 → 4 個程序（每來源一程序）或 → 3（每 data_dir 一程序，含 list-dicts），約回到 v0.7.1 的 47 ms；長句估計 121 → 約 10 個程序，從 1.2 s 降到 ~0.2–0.3 s。需在裝置上以 `tests/benchmark_lookup.py` 實測。 |
| 記憶體 | 與現狀相同（程序隨請求結束）。 |
| 複雜度 | 中：stdin 模組改成 frame 迴圈（約 100–150 行變動），`SdcvClient` 的三個查詢函數改為對同一 session 呼叫；gate 新增 4–5 個 case。`_lookup_modes()` 不動。 |
| 私隱 | 與現狀相同：查詢只經 stdin，`SDCV_HISTFILE=/dev/null`，程序隨請求結束，readline 記憶體歷史一併消失。 |
| 風險 | choice prompt 後 sdcv 印出 `]\n` 再印主提示，frame 邊界要用「主提示」而非「JSON 結尾」判定；stdout cap 改為會話總量。逾時策略：單 frame 逾時 kill 整個會話，已取得的 frame 仍可用（與現在逐程序失敗的粒度相同或更細）。 |

### 方案 B：常駐 sdcv

每個（data_dir，字典集合）組合維持一個長駐 `SdcvSession`，跨請求重用。

| 面向 | 評估 |
|---|---|
| 延遲 | 省去每請求的 fork/exec 與索引載入（約 10–15 ms × 程序數），單詞查詢可到 10–20 ms；長句與 A 的第二步相當。 |
| 記憶體 | 比想像中小：`OffsetIndex` 只常駐 `.oft`（每詞 4 bytes）與頁快取；`.idx.gz` 字典則整個索引常駐（視字典而定，數 MB 至數十 MB）。需在裝置上以 `/proc/<pid>/status` 實測。 |
| 複雜度 | 高：輸出切分（同 A）；逾時後重啟；config 或字典檔變動（mtime）重啟；每個 mode／data_dir 一組程序；程序的生命週期要掛在 resident runtime（`dank_lookup_runtime.py` 的 socket 服務）而非 one-shot CLI，`lib/backends/sdcv.sh` one-shot 路徑仍需 A 的實作作為後備；cancel 需要 `-1` 中止或 kill 重啟。 |
| 私隱 | **有新的代價**：readline 的 `add_history()` 把每個查詢與被選中的詞條留在程序記憶體，直到程序結束才寫入 `/dev/null`；常駐等於把查詢歷史放在一個長命程序的堆積裡（core dump、`/proc/<pid>/mem` 同 UID 可讀）。可用定期重啟（每 N 次查詢或 T 分鐘）緩解，但這與 `docs/privacy.md`「no query history」的敘述需要重新措辭。此外會話若因錯誤停在 choice prompt，狀態機必須能偵測並重啟。 |
| 建議 | 作為 A 之後的可選優化，且只在 resident runtime 啟用；先量測 A 是否已足夠。 |

### 方案 C：純 Python StarDict 後端

直接讀 `.ifo`／`.idx`（排序鍵為 `g_ascii_strcasecmp` 後 `strcmp`）／`.dict` 或 `.dict.dz`（dictzip：gzip ＋ `RA` extra field 的區塊表，可用 `zlib` 隨機存取）／`.syn`。

| 面向 | 評估 |
|---|---|
| 延遲 | exact：mmap `.idx` ＋ 二分搜尋，首次需要建立詞偏移表（Python 解析 30 萬詞約 0.2–0.5 s），需自建快取檔（類似 `.oft`）或常駐；之後每次查詢 <1 ms，且**零子程序**。fuzzy：sdcv 的演算法是對整個詞表逐詞計算編輯距離（C++ 下已是數十毫秒），純 Python 逐請求做不可行，必須常駐並預建索引（長度分桶、前綴、或 BK-tree），且結果順序要與 sdcv 的 `MAXFUZZY=10` 淘汰規則一致才能守住 fixture。 |
| 記憶體 | 若常駐：每字典詞偏移表（Python list/array，30 萬詞約 1–3 MB array，或用 `.oft` 格式 mmap）；dictzip 區塊表很小。 |
| 複雜度 | 高：格式解析（含 `idxoffsetbits=64`、`sametypesequence`、`.syn`）、與 sdcv 的 `LookupSimilarWord`（大小寫變體、`-s/-ed/-ing` 等後綴規則，`stardict_lib.cpp:1026-1330`）及 fuzzy 的行為對齊、dd5af8e 建立的「sdcv evidence contract」要重新定義（fixture 的預期值全部來自 raw sdcv 輸出）。 |
| 私隱 | 最佳：沒有子程序、沒有 readline、沒有任何歷史機制；查詢只存在於 Python 記憶體。 |
| 建議 | 長期方向，特別適合 exact-only 來源（`han`、`ja`、`yue` 模式的來源全部 `exact_only: true`）。可考慮混合：exact-only 來源走 C、`exact_only: false` 的來源走 sdcv。但這會讓一次 lookup 混用兩種後端的警告碼與 evidence，建議等 A 落地、介面穩定後再評估。 |

### 方案 D：Related 延遲載入

core 先回傳 exact，Related（fuzzy ＋ 候選回查）在使用者展開時才查。

| 面向 | 評估 |
|---|---|
| 延遲 | 只影響 `exact_only: false` 的來源（目前只有 `en` 模式的 GCIDE／WordNet，以及 v1 config 的 fallback）。在 A 之後，同程序內的 fuzzy ＋ 10 個回查約多 10–20 ms，收益有限；在現狀下則可省掉「Linux」21 個程序中的約 14 個。 |
| 複雜度 | 需要新的請求類型（可沿用 `open-ref` 再取回的模式）、QML 端的「載入中」狀態、以及 `mode_preferences.show_related` 預設為 false 時的體驗設計；fixture 要分成兩段。 |
| 建議 | 不作為本輪方案；若 A 落地後 `en` 模式仍明顯慢於其他模式再考慮。 |

### 總表

| | A 批次會話 | B 常駐 | C 純 Python | D 延遲 Related |
|---|---|---|---|---|
| 「Linux」估計 | ~50 ms | ~15 ms | exact <5 ms；fuzzy 視實作 | 現狀下 ~100 ms |
| 新增常駐記憶體 | 無 | 每組程序數 MB 起 | 常駐時每字典 1–3 MB | 無 |
| 改動範圍 | stdin 模組 ＋ `SdcvClient` | A ＋ runtime 生命週期 | 新後端 ＋ fixture 契約 | runtime ＋ QML |
| 私隱 | 同現狀 | 退步（記憶體歷史、長命程序） | 進步 | 同現狀 |
| fixture 風險 | 低（語義一對一） | 低 | 高 | 中 |

## 6. 建議路線與驗證

1. **抽介面**：新增 `lib/dank_lookup_backend.py`（Protocol ＋ 工廠），`lookup()` 只改型別與 `close()`；`tests/test_core.py` 的 fake 改為明確實作該 Protocol。這一步不改行為，可獨立 commit。
2. **stdin 模組改 frame 迴圈**：新增 `run_bounded_session()`／`SdcvSession`，保留 `run_bounded_protocol()` 作薄包裝；`tests/probe_sdcv_stdin.py` 的 gate 加入多 frame case，並把 spec／expected 一併更新（這是 dd5af8e 契約的一部分，必須有證據）。
3. **`SdcvClient` 改用會話**：先每來源一程序；跑 `tests/run_backend_fixtures.py` 全部 fixture 目錄，確認 entry_id、available_modes、default_mode 與修正前逐位相同。
4. **量測**：在裝置上以 Remote Control 跑 `tests/benchmark_lookup.py`（使用者 config 副本、臨時 HOME），比較 v0.7.1／c2cef49／本方案的 wall、backend_ms 與程序數；同時以 `strace -f -e execve` 或 `FAKE_CALL_LOG` 類的計數確認程序數。驗收門檻：單詞不高於 v0.7.1，長句不高於 v0.7.1，8/8 與 11/11 token 都有結果，`lookup_budget_exhausted` 不再出現。
5. **再決定**是否做每 data_dir 一程序（A 第二步）、B 或 C。
6. 同時把 §3 的 `/`、`|`、`*`、`?` 輸入語法問題記入 issue，決定跳脫或標記策略。

## 7. 開放問題

- choice prompt 在多字典會話中的編號是跨字典合併的（`res_list` 順序＝字典載入順序＝`--use-dict` 順序）；A 第二步要靠候選行的字典名分流，而非靠 index 範圍。
- stdout cap 的語義：現在是每個 lookup 的總量在 `SdcvClient.query()` 內逐程序扣減；改為會話後，cap 應改為會話總量，並在超出時 kill 會話、保留已解析 frame，警告碼不變。
- `source_timeout_ms`（現 1.5 s）現在是每程序逾時；改為會話後建議改為每 frame 逾時，整體仍受 `lookup_timeout_ms` 約束。
- 需要在裝置上確認目前安裝的 sdcv 是否連結 readline（`ldd $(command -v sdcv) | grep readline`），這決定 B 的記憶體歷史問題是否實際存在，以及提示是否回顯查詢（現有 parser 假設回顯）。

## 8. 實作狀態（2026-11-02，雲端分支 `claude/project-thread-901dgd`，基底 f64ac87）

已實作方案 A 的第一步，改動限於 stdin 模組、`SdcvClient` 與 `lookup()` 的收尾：

| 檔案 | 改動 |
|---|---|
| `lib/dank_lookup_sdcv_stdin.py` | `parse_protocol_stdout()` 拆出 `parse_frame()`（解析兩個主提示之間的一個 frame）；新增 `SdcvSession`（`ask(query, choice, timeout, cap)` → `(run, frame)`，`close()`）。`run_bounded_protocol()` 原樣保留，gate 與 `fixtures-sdcv-stdin` 不受影響。 |
| `lib/dank_lookup_core.py` | `SdcvClient` 以 `(字典, data_dir, exact 旗標)` 為鍵維持會話；`_stdin_query()` 改為 `session.ask()`；`query()` 的政策（exact → 同形詞 → `/fuzzy` → 候選回查、`candidates` 篩選、cap、預算）逐行不變。會話預設存活到 `lookup()` 結束（`session_scope="lookup"`），所以分詞後每個 token 共用同一組程序；`lookup()` 在 `finally` 呼叫 `client.close()`，關閉異常以 `sdcv_reap_failed`／`sdcv_execution_failed` 警告呈現。注入 `protocol_runner` 的測試替身經 `_OneShotSession` 包裝，既有 fake 不用改。 |
| `tests/test_core.py` | `SdcvSessionTests`：同程序多 frame、choice、`-1` 中止、frame 逾時後重啟、stdout cap、每來源程序數、跨 query 共用會話。 |
| `tests/probe_sdcv_session.py` | 新探針，對真 sdcv 0.5.5 以隔離字典證明：同程序 exact→fuzzy→候選→miss；`--exact-search` 下 `/fuzzy` 仍列候選；同形詞 `-1` 中止後逐 index 取回。**需在 Nocehi 電腦執行**，雲端容器無 sdcv。 |

程序數（假 sdcv 計數，`tests/test_core.py::SdcvSessionTests`）：

| 情境 | c2cef49 | 本實作 | `fuzzy_in_exact_session=True` |
|---|---|---|---|
| 一個 `exact_only: false` 來源，exact 命中 ＋ 10 個候選 | 12 | 2 | 1 |
| `exact_only: true` 來源，同形詞 2 條 | 3 | 1 | 1 |
| 「Linux」，GCIDE ＋ WordNet | 21 | 6（含 `--list-dicts` ×2） | 4 |
| 分詞 8 token × 同一組來源 | 每 token 重開 | 每來源一組，跨 token 共用 | 同左 |

`fuzzy_in_exact_session` 預設關閉：sdcv 原始碼顯示 `/` 查詢不受 `--exact-search` 影響（`libwrapper.cpp` `process_phrase` 的 `qtFUZZY` 分支），但現行 gate 只證明過不帶 `-e` 的模糊 frame；`probe_sdcv_session.py` 的 `session_fuzzy_under_exact_search` 通過後即可改預設。

雲端容器無法執行的驗證：`probe_sdcv_stdin.py` gate、`probe_sdcv_session.py`、`run_backend_fixtures.py`（需要真字典）、`benchmark_lookup.py`。容器內 `test_cli`／`test_transport` 有 6 個與本改動無關的既有失敗（`posix_spawn: setsid unavailable`、程序群組無法回收），改動前後相同。

## 9. 現階段仍存在的 bottleneck

以 c2cef49 的量測（Linux 267 ms、春 179 ms、中文長句 1.2 s）與每個 sdcv 程序約 10–15 ms 的固定成本推算：

1. **每請求仍要重新啟動程序**：單詞查詢的下限約為「`--list-dicts` ×data_dir 數 ＋ 每來源 1–2 個程序」× 10–15 ms，即 Linux 約 50–70 ms，與 v0.7.1 的 47 ms 同級但不會更低。要再降只能（a）每個 data_dir 一個程序（`--use-dict` 多字典，候選行按字典名分流，預估 Linux 約 30 ms）或（b）常駐（方案 B，約 10–20 ms，見 §5 的私隱代價）。
2. **`--list-dicts` 每請求兩個程序**：`source_installed()` 的快取只在 `SdcvClient` 實例內，one-shot backend 每次新建實例。sdcv 掃描 data_dir 內所有 `.ifo`，約 10 ms × 2。可改為讀 `.ifo` 的 `bookname` 自行判斷（core 已有 `_read_ifo_metadata()`），零程序。
3. **sdcv 自身的模糊搜尋**：`LookupWithFuzzy` 對整個詞表逐詞算編輯距離（`stardict_lib.cpp:1347`），GCIDE／WordNet 規模下每次數十毫秒，且 `exact_only: false` 的來源每次都做。這是 `en` 模式比其他模式慢的主因，會話化之後仍在；方案 D（Related 延遲載入）或純 Python 前綴索引才能避開。
4. **CJK 查詢仍跑 en-en 的 fallback 模糊搜尋**（v1 config 語義，phase2 §6 第 4 點），每次多 2 個程序 ＋ 2 次全詞表編輯距離。屬 config 政策，不在後端層。
5. **分詞路徑**：tokenizer 本身一個程序，加上每 token 的 frame 數（來源數 × 1–3 frame）。會話化後長句估計由 1.2 s 降到 0.3 s 以下，剩餘主要是 tokenizer 啟動與 frame 的串行等待；frame 之間可考慮並行多個來源的會話（目前串行）。
6. **逾時粒度**：`source_timeout_ms` 現在是每 frame 逾時；一個卡住的 frame 會 kill 整個會話並在下一個 frame 重啟（成本等同舊行為），不會更差，但也沒有更好。
7. **同形詞仍需「中止 ＋ 逐 index 回查」**：互動模式無法一次印出同字典多條結果，每多一條同形詞多一個 frame（約 1 ms），已不是瓶頸。

建議的下一步順序：先在裝置上跑 `probe_sdcv_session.py` 與 `benchmark_lookup.py` 取得實測；通過後開啟 `fuzzy_in_exact_session`；再做第 2 點（免程序的 `--list-dicts`）與第 1(a) 點。
