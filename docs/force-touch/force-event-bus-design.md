# Force 事件匯流設計

2026-11-06。狀態：設計稿第三版（依 Nocehi 審閱修訂認領競態、多指判定、預先查詢評估，並加入第三階段的量化門檻），另加第十節可行性探路；未寫程式，未改動 repo 或機上安裝。

## 零、方向與決定

Apple 的查詢、Quick Look、資料偵測，全靠應用回報游標下的物件；監聽器在任意應用裏拿不到這個資訊（見 [survey 第三節](apple-force-touch-survey.md)）。但在 DMS 自己的介面裏，shell 完全知道游標停在哪個 Dock 圖示、哪則通知、哪條剪貼簿紀錄上。Apple「應用回報物件、系統提供手勢」的模型，在 Linux 上唯一能完整複製的地方，就是 shell 本身。

因此方向調整如下（Nocehi 2026-11-06 決定）：

1. **監聽器改為 force 事件匯流**，只負責三件事：判定深壓、發觸覺、廣播事件。它不再直接呼叫 `dank-lookup-selection`；Dank Lookup 成為消費者之一。「深壓查詢」與「Dock 深壓」從此是同一套程式的兩個消費者。
2. **DMS 元件在放開時才執行動作**（同 macOS Dock）。元件在按下時認領按壓，放開時以匯流的 `release` 判定是否深壓；是深壓即視為「已消耗」，不執行原本的點擊動作（第四節）。
3. **放棄 niri 欄寬吸附觸覺**：欄寬與工作區切換已交還鍵盤，觸覺落在一隻沒在動的手下面，沒有意義。
4. **資料偵測提前**：與硬件無關，鍵盤快捷鍵觸發的查詢一樣受惠，列為最高優先之一。
5. **Firm Press（接觸面積）**：已在「Verify contact area for deep press」thread 驗證並放棄。90 次樣本（`area-probe/`）顯示接觸面積在點擊後即趨平，普通點擊與深壓在重疊區的面積無異；單用壓力與壓力加面積的分辨率相同。本設計只用壓力。

## 一、已查證的前提

| 項目 | 結論 | 依據 |
|---|---|---|
| quickshell 有通用 D-Bus QML 介面嗎 | **沒有**。`src/dbus/` 只有內部用的 Properties、ObjectManager 封裝，供 MPRIS、通知、系統匣等服務使用，無 `QML_ELEMENT` | quickshell 原始碼 `src/dbus/CMakeLists.txt`、`bus.hpp`、`properties.hpp` |
| quickshell 能當 unix socket 客戶端嗎 | **能**。`Quickshell.Io.Socket`：`path`、`connected`、`write()`、`flush()`，繼承 `DataStream`，可配 `SplitParser` 逐行解析；另有 `SocketServer` | `src/io/socket.hpp`；編譯選項 `SOCKETS` 在 v0.3.2 預設開啟。機上 0.3.2 已間接核實，見 10.1 |
| DMS 已有 socket 用法嗎 | **有**。`Services/DMSService.qml` 以 `DCommon.DSocket` 連 `DMS_SOCKET`（DMS 的 Go 後端），含斷線重連與 subscribe 協定 | DMS 原始碼 |
| DMS Dock 是按下還是放開時啟動 App | **已經是放開時**。`DockAppButton.qml` 的左鍵動作 `handleLeftClick()` 在 `onReleased` 內執行；`onPressed` 只記錄拖曳起點並啟動 500 ms 長按計時（釘選 App 的拖曳重排） | DMS **v2.0.0 tag** 與 master 的 `quickshell/Modules/Dock/DockAppButton.qml` |
| 其他 DMS 元件 | Qt `MouseArea.onClicked` 本身就在放開時觸發，所以多數元件天然符合；只有把動作寫在 `onPressed` 的地方要改。**尚未逐一盤點** | Qt 文件語義；盤點列為第三階段工作 |
| 游標的螢幕座標 | 監聽器只有觸控板座標（`ABS_MT_POSITION_X/Y`），與螢幕位置無關；libinput 不外露，niri IPC 據我們所知沒有游標位置查詢（未在 26.04 核實） | 見 force-touch-haptics.md 1.1 |

第一項與第四項是整個設計的關鍵：消費端不能用 D-Bus，只能用 socket；而 Dock 已在放開時才動作，「已消耗」的做法在 DMS 端只需要很小的改動。

## 二、傳輸方式

| 方案 | 雙向 | quickshell 端 | 延遲 | 評估 |
|---|---|---|---|---|
| D-Bus 訊號 | 是（訊號＋方法） | 無 QML 介面，要嘛 `Process` 跑 `dbus-monitor` 再解析文字，要嘛寫 C++ 外掛 | 低 | **否決**：消費端成本最高 |
| **Unix socket，換行分隔 JSON** | 是 | `Socket` + `SplitParser`，DMS 已有 `DSocket` 封裝可沿用 | 亞毫秒 | **採用** |
| `dms ipc call` / `qs ipc call` → `IpcHandler` | 單次呼叫有回傳值 | 現成 | 每事件起一個程序，數十毫秒級（未量測） | 只適合做零基建原型；不能廣播，無法讓多個 quickshell 實例同時收事件 |
| niri IPC | 否 | — | — | **否決**：niri 不能轉發自訂事件 |
| 併入 DMS Go 後端（`DMS_SOCKET` 的一個 subscribe 服務） | 是 | 現成 | 低 | 長期選項，需上游接受；evdev 讀取與 hidraw 寫入要搬進 Go。先不做 |

Socket 位置：`$XDG_RUNTIME_DIR/dank-force/bus.sock`。目錄 0700、socket 0600，只有本人可連。事件不含任何文字。

匯流同時是 hidraw 觸覺節點的唯一擁有者。消費者需要觸覺時（例如卡片內的第二次深壓），向匯流請求，而不是各自開 `/dev/hidraw3`。

## 三、事件格式（v1）

每行一個 JSON 物件。`id` 是每次按壓遞增的整數；`t` 是匯流的單調時鐘毫秒，只供除錯與排序。

### 3.1 匯流 → 消費者

| `type` | 時機 | 欄位 |
|---|---|---|
| `hello` | 連線後立即 | `v: 1`、`device`（如 `"05ac:0324"`）、`haptic`（布林，是否 USB 且可寫）、`force`、`hold_ms` |
| `press` | `BTN_LEFT` 按下 | `id`、`t`、`touches`（接觸點總數）、`ignored`（被當作擱置手指而忽略的接觸點數） |
| `progress` | 按住期間壓力介於預熱值（約 110）與門檻之間；僅發給訂閱者，限 30 Hz | `id`、`value`（0–1，即 (p − 110) / (160 − 110)，對應 Apple 的 `stageTransition`） |
| `deep` | 達到 stage 2（主觸點 ≥ 160 持續 80 ms），觸覺已發 | `id`、`t`、`click_to_deep_ms`、`peak` |
| `cancel` | 本次按壓不可能再成為深壓 | `id`、`reason`：`drag`（主觸點位移超過門檻）、`multitouch`（第二個觸點也在移動或施壓，見 3.3）、`device_lost` |
| `release` | `BTN_LEFT` 放開，**立即發出，不設寬限** | `id`、`t`、`deep`（布林，**本次是否深壓的唯一權威**）、`owner`（認領者名稱或 `null`）、`cooldown`（布林，深壓但落在冷卻期內） |
| `default` | 與 `release` 同時，僅在深壓且無人認領時，只發給一個 `default` 角色的消費者 | `id` |
| `claim_result` | 回覆認領 | `id`、`ok`、`owner` |

`press`、`deep`、`cancel`、`release` 對所有消費者廣播。拖曳抑制（比照 Apple `primaryDeepClick`）在匯流內完成：主觸點位移超過約 3 mm 即發 `cancel`，之後不會再有 `deep`。

觸控板座標（`pad: [x, y]`，0–1 正規化）只在 `-v` 除錯模式附在 `deep` 上。它不是螢幕座標，消費者不得用它判斷目標。

### 3.2 消費者 → 匯流

| `type` | 用途 | 欄位 |
|---|---|---|
| `hello` | 宣告身分與角色 | `client`（如 `"dms"`、`"dank-lookup"`）、`roles`：`claimant`、`default` 其一或兩者、`want`：可選 `["progress"]` |
| `claim` | 宣告「這次按壓落在我的元件上」，**在按下時送出**，不等深壓 | `id`、`owner`（如 `"dms.dock"`） |
| `haptic` | 請求觸覺 | `pattern`：`generic`、`level`、`alignment`；匯流限速（每 100 ms 至多一次） |

### 3.3 主觸點與擱置手指

壓力樣本全是單指，但實際用法是觸控板放在空白鍵前方，打字時拇指常擱在板的下緣，食指深壓時就是兩點接觸。所以匯流不以「單指」為條件，改為：

1. **主觸點**＝壓力最高的觸點，只對它判定門檻、維持時間與位移。
2. 其他觸點若**靜止**（自接觸起位移小於約 1 mm）且**低壓**（< 50），視為擱置手指，忽略。
3. 只有第二個觸點也在移動，或壓力 ≥ 50 時，才判定為多指手勢，發 `cancel: multitouch`。

門檻 50 取自輕放不按的上緣（11–51），但擱置的拇指可能比輕放的指尖重；第一階段以 `-v` 日誌記錄每次按壓的觸點數、被忽略數與被忽略觸點的最高壓力後再校準。

## 四、認領規則

核心原則：**目標由消費者自己判斷；「是不是深壓」只由匯流的 `release` 決定。**

### 4.1 認領在按下時進行

消費者的元件被左鍵按下時（`MouseArea.onPressed`），把這次 Qt 按壓與匯流的 `press` 配對，並立即送出 `claim`。配對方式：兩者到達先後不定，任一方先到即等另一方，相隔 50 ms 內者視為同一次按壓；50 ms 內等不到 `press`，表示這次按壓不是來自觸控板（例如滑鼠），照常處理，不認領。

認領在按下時送出，距離最早可能的深壓（點擊後 75 ms 再加 80 ms 維持）至少約 155 ms，遠多於 socket 往返的毫秒級延遲。因此匯流在放開時已經知道有沒有認領者，不需要任何寬限時間。普通點擊也會被認領，無害：`release` 會帶 `deep: false`。

### 4.2 放開時以匯流為準

Qt 的 `onReleased` 走 evdev → libinput → niri → Wayland → quickshell，匯流的 `release` 走 evdev → 匯流 → socket，兩條路徑誰先到並不確定。若消費者自己看 `deep` 事件來決定，使用者在深壓後立即放手時，Dock 可能先收到放開並啟動 App，`deep` 才到，兩個動作都執行。所以：

1. 已配對的按壓，`onReleased` **不立即**呼叫 `handleLeftClick()`，而是等同一 `id` 的 `release`。
2. `release` 若已先到（暫存在服務內），立即處理；否則最多等 80 ms，逾時照常執行點擊。
3. `deep: false` → 照常執行點擊。`deep: true` → 不執行點擊；深壓動作（如 App Exposé）已在收到 `deep` 時執行，比照 Apple 在深壓當下顯示。
4. 未配對的按壓（滑鼠、匯流未連線）不等待，照常執行。

Dock 啟動 App 本來就不講求毫秒，多等數十毫秒察覺不到，卻能完全消除雙重觸發。原稿的「先在本地標記已消耗、萬一被拒再清除」與放開後 30 ms 寬限一併刪除。

時序：

```
BTN_LEFT 按下 ── press(id, touches, ignored) ──▶ 全部
     │            DMS：onPressed 與 press 配對 → claim(id, "dms.dock")
     │◀── claim ──   （此時離最早的深壓還有 ≥ 155 ms）
     │── claim_result(ok) ──▶ DMS
主觸點 ≥160 持續 80 ms
     │── 觸覺脈衝
     │── deep ──▶ 全部
     │            DMS：執行深壓動作（App Exposé）
BTN_LEFT 放開
     │── release(id, deep: true, owner: "dms.dock") ──▶ 全部（立即）
     │            DMS：onReleased 等到此 release（或已暫存）→ 不執行點擊
     └── 若深壓而無人認領：default ──▶ Dank Lookup；無 default 消費者連線 → 執行 dank-lookup-selection
```

### 4.3 細則

1. **先到先得**。同一 `id` 的第二個認領收到 `ok: false`。游標只在一處，正常情況下不會發生。
2. **遲到的認領**：若認領在 `release` 之後才到（理論上只在 socket 嚴重阻塞時發生），匯流已走後備鏈；記錄為 `late_claim` 並計數，不另作補救。
3. **後備鏈**：認領者 → `default` 角色消費者（Dank Lookup，查 PRIMARY 選取）→ 匯流自己執行 `dank-lookup-selection`（等同今日行為）。任何階段都不會比現在差。
4. **冷卻**留在匯流（400 ms），只套用於後備鏈；認領者自行決定是否需要冷卻。
5. **與 Dock 長按拖曳的衝突**：釘選 App 按住 500 ms 會進入拖曳模式，而實測點擊到深壓需 75–606 ms。規則：收到 `deep` 時若 `longPressing && !dragging`，取消長按並執行深壓動作；若已在拖曳，忽略 `deep`，且該次 `release` 照拖曳邏輯處理（拖曳中的深壓屬 `primaryDeepDrag`，目前不做）。
6. **降級情況**：DMS 未連上匯流時，在 Dock 上深壓會照舊啟動 App，同時後備鏈查詢 PRIMARY 選取。這與今日行為相同，可以接受。

## 五、各方改動

### 5.1 匯流（新程式，取代 `dank-force-press-watch`）

- 保留現有判定、觸覺、裝置重掃描、冷卻；新增 socket 伺服器（Python `selectors`，與 evdev 讀取共用一個事件迴圈，不需執行緒）。
- 判定由「單指」改為「主觸點＋忽略擱置手指」（3.3）。
- 新增拖曳抑制、`progress` 計算、按下時認領、`--monitor` 模式（連上 socket 印出事件，取代 `socat` 除錯）。
- 每次按壓在日誌寫一行統計（只有數字，不含文字）：觸點數、被忽略數、被忽略觸點最高壓力、`progress` 是否越過 0.6、是否成為深壓、越過 0.6 到深壓的毫秒數、認領者、是否 `late_claim`、深壓後重試配對（`retry_of`、`gap_ms`、`move_mm`，見 6.1）。供校準擱置手指門檻與評估預先查詢。
- 參數不變：`--force 160 --hold 80 --cooldown 400 --strength 0x26C19`；後備指令沿用 `--cmd`。
- 隱私邊界不變：只處理壓力、時間、觸控板座標，從不讀取或記錄文字。

### 5.2 dms-lookup（本 repo）

| 改動 | 說明 |
|---|---|
| `ForceBusClient`（QML，standalone shell 內） | `Socket` 連匯流，`hello` 宣告 `roles: ["default"]`、`want: ["progress"]` |
| `default` 事件 | 走與快捷鍵相同的入口（現有 `prepare` → `capturePrepared` 流程），不另開路徑 |
| 預先查詢 | `progress` 過約 0.6（壓力約 140）時在背景讀選取並查詢；收到 `default` 只負責顯示；收到 `cancel` 或 `release` 且非本消費者負責時丟棄結果。只在本機進行。保留與否以數據決定，見 5.4 |
| 卡片內第二次深壓（稍後） | 卡片開啟且游標在卡片上時，以 `claimant` 身分認領，展開完整條目，並請求 `level` 觸覺 |
| 設定項 | 深壓、觸覺、預先查詢各自可關（HIG 要求） |
| 資料偵測 | 與匯流無關，見第七節 |

### 5.3 DMS（上游 AvengeMedia/DankMaterialShell，不在本 repo）

| 改動 | 說明 |
|---|---|
| `Services/ForceTouchService.qml`（singleton） | 沿用 `DCommon.DSocket` 連匯流；負責 Qt 按壓與 `press` 的配對、送出 `claim`、暫存各 `id` 的 `release`；提供 `deep(id)` 訊號與 `awaitRelease(id, timeoutMs, callback)`；匯流不存在時靜默停用（所有按壓視為未配對） |
| `DockAppButton.qml` | `onPressed` 向服務登記並認領；收 `deep` 時打開該 App 的視窗列表；`onReleased` 對已配對的按壓改為 `awaitRelease(id, 80, …)`，依 `deep` 決定是否呼叫 `handleLeftClick()`；處理長按衝突（4.3 第 5 點） |
| 盤點其他元件 | 找出把動作寫在 `onPressed` 的地方，改到放開時；啟動器（深壓叫出快捷操作）、通知（展開）、剪貼簿歷史（預覽完整內容）、狀態列小工具依次接入 |

**2026-11-06 Nocehi 決定：本機修補，形式為自己的 DMS git checkout ＋ 修補分支。** 已查證（原始碼與機上唯讀檢查）：

| 項目 | 結果 |
|---|---|
| 套件 | `dms-shell 2.0.0-1`，CachyOS 官方 `cachyos` repo（非 AUR）；`quickshell 0.3.2-1.1` |
| UI 來源 | 內嵌在 `/usr/bin/dms`，每次啟動解壓到 `/run/user/1000/dms-shell/<rev>/`（唯讀），再以 `qs -p` 載入；現行 rev `496f460c1c70f6d9`。DMS 2.0 **不會**搜尋 quickshell 的 XDG config 目錄 |
| 覆蓋方式 | 依 `dux-go/shellapp` 的 `ResolveConfig`：`dms -c <目錄>` 優先，其次環境變數 `DMS_SHELL_DIR`，再其次是執行中實例的路徑，最後才是內嵌 UI。目錄內須有 `shell.qml`；repo 的 `quickshell/` 目錄符合 |
| 啟動方式 | systemd user unit `dms.service`（套件提供）執行 `dms run --session`；niri 不負責啟動。目前沒有 drop-in，也沒有設定 `DMS_SHELL_DIR` |
| 殘留 | `/usr/share/quickshell/dms` 不屬於任何套件，只有 1.x 時代的 `Pam.qml` 手動備份與翻譯檔，2.0 不讀取。是否清理由 Nocehi 決定 |

**狀態（2026-11-06 23:00 HKT）：已就緒。** checkout `~/src/DankMaterialShell`，分支 `local/force-touch` 位於 v2.0.0（`9b41329`），未加修補；drop-in `10-shell-dir.conf` 由 Nocehi 安裝，DMS 已改從 checkout 載入，日誌沒有新的錯誤類型。熱重載在此模式下是開啟的：改動或切換分支會即時重載 shell。

做法（原計畫，前三步已完成）：

1. `git clone --recurse-submodules` DMS 到 `~/src/DankMaterialShell`。`quickshell/DCommon` 是指向子模組 `dux-qml-common` 的符號連結，不加 `--recurse-submodules` 會缺檔。
2. 從與安裝版相同的 commit 開分支（先比對 `/run/user/1000/dms-shell/<rev>/.dankrev` 與 v2.0.0 tag；QML 必須與 `dms` 執行檔的後端協定同版）。
3. 加一個 drop-in `~/.config/systemd/user/dms.service.d/local-shell.conf`，內容為 `[Service]` 與 `Environment=DMS_SHELL_DIR=%h/src/DankMaterialShell/quickshell`，然後 `daemon-reload` 並重啟 `dms`。
4. 還原：刪除 drop-in，`daemon-reload`，重啟 `dms`，即回到內嵌 UI。
5. 升級：`pacman` 更新 `dms-shell` 後，把修補分支 rebase 到新版本對應的 tag。版本不一致時應先移除 drop-in，以免新執行檔載入舊 QML。

補充查證：
- 內嵌樹根目錄確有 `shell.qml`（`ShellRoot`，先載入 `ShellCore.qml`，再載入 `DMSShell.qml`），所以 `DMS_SHELL_DIR` 指向 checkout 的 `quickshell/` 即可。
- 內嵌版 `VERSION` 為 `v2.0.0`，`.dankrev` 為 `496f460c1c70f6d9`。`shell.qml` 要求 `DMSService.apiVersion >= 38`；`dms` 執行檔註明「比執行檔新的 shell 經 `DMS_SHELL_DIR` / `-c` 載入屬受支援的做法」。
- `DMS_DISABLE_HOT_RELOAD=1` 只出現在 `qs` 程序的環境裏，是 `dms` 後端啟動 `qs` 時自行注入的（內嵌樹唯讀）。改用 checkout 後是否仍注入，須實際跑過才知道。
- 執行時的驗證方法：`cat $XDG_RUNTIME_DIR/dms.path`、`ps -o args -C qs` 應指向 checkout；`journalctl --user -u dms -b` 不應有 QML 錯誤。重啟 `dms` 會讓桌面 shell 短暫重載。

### 5.4 預先查詢的評估

預先查詢保留，但以數據決定去留。匯流與 Dank Lookup 各記一半（只有數字）：

- 匯流：每次 `progress` 越過 0.6 計數一次，並記錄該次按壓最終是否成為深壓。一兩天後即可得出「普通點擊觸發背景查詢」的比例。
- Dank Lookup：深壓時記錄預先查詢結果是否已就緒，以及就緒時刻比 `default` 到達早多少毫秒，即實際省下的延遲。

若誤觸比例高而省下的延遲小，關閉此功能，不必再爭論。

## 六、遷移步驟

| 階段 | 內容 | 對使用者的影響 | 還原 |
|---|---|---|---|
| 0（現況） | `dank-force-press-watch` 直接呼叫 `dank-lookup-selection` | — | — |
| 1 | 匯流上線，**沒有任何消費者**，後備鏈直接執行 `dank-lookup-selection`；同時加入拖曳抑制 | 與現在相同，只少了拖選時的誤觸 | 換回舊檔 |
| 2 | Dank Lookup 以 `default` 角色連上，接手查詢並啟用預先查詢 | 卡片出現更快 | 關閉設定項，回到後備指令 |
| 3 | DMS `ForceTouchService` + Dock 深壓。**前提：深壓後重試率 ≤ 2%（6.1）** | Dock 可深壓 | 不載入該服務即回到第 2 階段 |
| 4 | 其他 DMS 元件逐一接入 | — | 逐項關閉 |
| 5 | 匯流、服務、udev 規則納入 repo | — | — |

### 6.1 進入第三階段的門檻：深壓後重試率

匯流本身無從得知某次深壓是不是誤判，所以用一個代理指標：**深壓後不久，在同一位置又出現一次普通點擊**，通常表示使用者其實只想點擊，正在重試。Dock 接入前，這個比例必須達標，而不只是「看過日誌」。

**配對方式。** 匯流只有觸控板座標，沒有螢幕座標；但用觸控板操作時，游標只會因觸點移動而移動。所以「同一位置」以「兩次按壓之間游標沒有實際移動」來判斷：

1. 起點：一次 `release` 帶 `deep: true`（含 `cooldown: true` 者；`cancel` 的不算，因為沒有成為深壓）。匯流記下其 `id` 與放開時刻。
2. 時間窗：放開後 **1 秒內**出現的下一次 `BTN_LEFT` 按下。
3. 位置容差：從放開到下一次按下之間，按鍵未按下時所有觸點的位移量累加（即會驅動游標的移動）**不超過 2 mm**。以裝置 `absinfo` 的 resolution 換算為觸控板單位；若驅動回報 resolution 為 0，先以原始單位記錄，校準後再定。手指離開再放下不算位移，只計放下後的移動。
4. 該次按壓本身**沒有**成為深壓（`release` 帶 `deep: false`），才算重試；連續兩次深壓屬於有意的重複動作，不計。
5. 任一條件不符，或 1 秒內沒有按下，該次深壓記為「無重試」。每次深壓至多配對一次。

**欄位一致。** 「Fix force-press drag misfire」正為現行監聽器加入約 3 mm 的拖曳門檻與每次按壓的位移記錄（Magic Trackpad 解析度約 47 單位/mm）。第一階段沿用同一欄位名稱與單位（mm），重試判定的 `move_mm` 與拖曳抑制的位移共用同一換算。

**記錄方式。** 每行按壓統計新增兩欄：普通點擊若符合上述條件，記 `retry_of=<深壓 id>` 與 `gap_ms`、`move_mm`；深壓一行則照舊寫出，事後由 `dank-force-bus --report` 讀 journal 彙總（只有數字，不含文字）。為方便校準，接近條件的情況也記錄（例如 1–2 秒內或 2–5 mm），但不計入比率。

**標準。** 深壓後重試次數 ÷ 所有深壓次數 **≤ 2%**，Dock 才接入。為免樣本太少，另加條件：**至少累積 100 次深壓**（此下限是本稿提議，Nocehi 可改）。

**未達標時**：回到第一階段，依日誌調整 `--force`、`--hold`，或改進判定（例如擱置手指、拖曳門檻），計數歸零重新累積。第二階段（Dank Lookup 接入）不受此門檻限制，因為查詢誤觸的代價只是多一張可關閉的卡片；Dock 誤觸則會吞掉一次啟動 App 的點擊。

**已知偏差**：查詢卡片彈出後，使用者若在原處點一下關閉卡片，也會被記為重試，使比率偏高（偏保守）。外接滑鼠移動游標時匯流看不到，可能把不同位置的點擊誤配為重試，同樣偏保守。

第一階段沿用 systemd 單元名稱 `dank-force-press`，只換 `ExecStart` 指向的程式，避免改動自動啟動設定。每一階段的機上安裝都需 Nocehi 明說可以。

匯流程式放在哪裏：暫放 dms-lookup repo（`bin/`、`systemd/`、`packaging/`，與 force-touch-haptics.md 原先提議的佈局一致），因為 Dank Lookup 是第一個消費者。匯流本身與查詢無關；若 DMS 上游日後接納，再拆成獨立 repo 或併入 DMS 後端。

## 七、資料偵測（Dank Lookup 功能，與匯流平行進行）

目標：選取內容不是詞語而是日期、數量或網址時，卡片顯示對應的本機結果。觸發方式不限於深壓，快捷鍵 Mod+Alt+D 一樣適用。守住「不上網」邊界。

| 類別 | 範例 | 卡片內容 | 備註 |
|---|---|---|---|
| 日期 | `2026-11-06`、`6 Nov 2026`、`十一月六日` | 星期、距今天數、其他曆法格式 | `11/06` 這類有歧義的寫法，兩種讀法都列出 |
| 時間與時區 | `15:00 UTC`、`3pm PST` | 換算為本機時區 | 依系統 tzdata，離線 |
| 單位 | `5 ft 10 in`、`350°F`、`2.5 kg` | 換算為公制或英制 | 不做貨幣：匯率需上網 |
| 網址 | `https://…`、裸網域 | 拆開顯示網域（突出顯示，防仿冒）、路徑；「複製」按鈕 | 不抓取網頁，不預覽內容；複製只在按鈕按下時寫入剪貼簿，不讀取剪貼簿 |

偵測失敗或不符合任何類別時，照常走辭典查詢；偵測結果與辭典結果可並列（例如 `May` 既是月份也是詞語）。

## 八、未決事項

1. ~~機上 quickshell 0.3.2 是否以 `SOCKETS` 編譯~~：已間接核實（10.1）。第二階段開工時順手以最小 QML 測試再確認一次即可，不再是前提。
2. DMS 修補走本機還是上游 PR（第三階段前由 Nocehi 決定）。
3. 門檻裕度：Firm Press 驗證的樣本中，部分普通點擊峰值達 198，高於先前量得的 ≤ 148。門檻 160 加 80 ms 維持能否擋住，以 6.1 的深壓後重試率衡量；未達標則提高 `--force` 或 `--hold`。
4. Firefox 與 Qt 程式在深壓後 PRIMARY 選取是否保留（原待辦第 3 項，仍有效）。
5. 拖曳抑制的位移門檻（暫定 3 mm）與擱置手指的壓力門檻（暫定 50），以第一階段的日誌校準。
6. 預先查詢去留：依 5.4 的統計，試用一兩天後決定。
7. `awaitRelease` 的 80 ms 超時：若日誌顯示匯流 `release` 常比 Qt 放開晚到超過 80 ms，再調整。

## 九、評估：多級壓力、三指 Force、剪貼簿後備（2026-11-06）

### 9.1 多級壓力與多級觸覺：不做

依據 `margin-probe/`（34 次）與 `area-probe/`（90 次）：

| 量 | 普通點擊 | 深壓 |
|---|---|---|
| 壓力 ≥ 160 的最長連續時間 | ≤ 44 ms | 265–818 ms |
| 峰值 | 一般 ≤ 157，個別 171、198 | 176–253 |

- **壓力軸**：深壓峰值常只到 176–184，第三級（例如 ≥ 225）多數深壓根本到不了，而能到的已接近 253 的上限。不可行，與原結論一致。
- **時間軸**：時間是目前分辨點擊與深壓最可靠的維度（44 ms 對 265 ms），但同樣的數據也說明第二級不能靠時間：普通深壓本身就持續 265–818 ms，5 次中有 3 次超過 400 ms。要避開自然深壓，門檻得放到 1 秒以上，那已是「長按」而非「用力按」，又與 Dock 釘選圖示的 500 ms 長按拖曳相撞。深壓樣本只有 5 次，這個判斷可以用第一階段日誌再確認，但方向不會變。
- **Apple 的做法**：macOS 也只用一個額外階段做查詢；多級壓力只用在 `NSMultiLevelAcceleratorButton` 這類連續控制項。iPhone 的 Peek and Pop（兩級）已收回為長按。
- **觸覺**：維持一種脈衝。日後若有「卡片內第二次深壓」（9.4），可用較弱的 `level` 式脈衝區分，不需要多級壓力。

### 9.2 三指 Force：不做

1. **三指點擊在 Linux 上是中鍵**。libinput 對 Apple 觸控板預設 clickfinger：二指點擊＝右鍵，三指點擊＝中鍵；而中鍵在大多數程式與終端機裏是「貼上 PRIMARY 選取」。三指深壓的第一段點擊就會把選取的文字貼進焦點視窗，這比誤觸查詢嚴重得多。（若 niri 設定改了 click-method，情況不同；尚未查證機上設定。）
2. **與 niri 手勢相撞**：三指滑動是 niri 的工作區與欄切換手勢，按壓時稍有位移便會觸發。
3. **與現行判定相反**：匯流只看壓力最高的觸點並忽略擱置手指（3.3），三指則要求三個觸點都在施壓；每指分到的壓力較低，門檻須另行量測，現有樣本全是單指。
4. **Apple 的三指是替代入口，不是疊加**：macOS 的「三指輕點查詢」是給不用 Force Click 的人的另一種觸發方式，不是第二級。

### 9.3 Preview 與完整視窗

Dank Lookup 現有的「Preview」是**內容層面**的：條目過長時卡片顯示截斷內容，標示「Preview · full entry」，按「Open full」經 `open-ref` 讀完整條目。卡片本身是 `FloatingWindow`（niri 一般視窗），只能以 Esc、關閉鈕或 IPC `close` 關閉，沒有「放開即消失」的輕量模式，也不會在失去焦點時自動關閉。

Nocehi 的構想真正要解決的，是「每次查詢都要動手關窗」。這可以不靠第二級手勢解決。

### 9.4 建議：先做「降低關窗成本」這一半

**2026-11-06 Nocehi 同意第 1、2 項。**

1. **卡片開啟時再深壓一次**（游標在卡片上）：Dank Lookup 以 `claimant` 認領，展開完整條目（即現在的 Open full）；游標不在卡片上時的深壓，則以新的選取取代卡片內容。兩次深壓在時間上分開，不需要第二個壓力級。
2. **可選的自動關閉**：失去焦點或點擊卡片外時關閉，做成設定項，預設關閉，先自用試用。
3. **「按住預覽、放開消失」**（iPhone Peek 式）暫不做。它要求卡片改用 layer-shell 浮層，避免 niri 把它當成視窗排版和搶焦點，且與「放開時才查詢」的現行時序相反。等第 1、2 項與預先查詢的數據出來後再評估。

### 9.5 剪貼簿後備：是現況，深壓路徑應移除

現況：`bin/dank-lookup-selection` 先讀 PRIMARY；PRIMARY 為空或讀取失敗時，**讀一次一般剪貼簿**作後備（`README.md`、`docs/privacy.md` 均有記載）。常駐 UI 從不讀剪貼簿，這一點沒有變。

問題出在深壓路徑：

- 沒有選字就深壓，卡片會顯示最後複製的內容。若那是密碼管理器剛複製的密碼，它會以明文出現在螢幕上。用快捷鍵時使用者知道自己在查什麼，深壓則容易在沒選字時誤觸（第 6.1 節的重試率正是為此而設）。
- 預先查詢（5.4）在普通重按時也會讀取；若沿用後備，等於每次重按都可能讀一次剪貼簿，違背「不預先讀取」的隱私原則。

建議：

1. **深壓與預先查詢只讀 PRIMARY**：`dank-lookup-selection` 增加只讀 PRIMARY 的選項（例如 `--primary-only`），匯流的後備指令與 Dank Lookup 的 `default` 角色都使用它。這應在第一階段一併完成。
2. **快捷鍵路徑**暫時保留剪貼簿後備，因為有些程式不設 PRIMARY，而且使用者是明確地按下快捷鍵。是否連快捷鍵也移除，由 Nocehi 決定；若移除，`docs/privacy.md` 要同步修改。

## 十、可行性探路（2026-11-06 晚）

範圍：匯流、DMS Dock 接入、卡片內第二次深壓展開三項，只查證，不寫功能程式。機上部分為唯讀檢視 `~/src/DankMaterialShell`（`local/force-touch`）與 `~/dank-lookup`（`6bcd478`）。

### 10.1 本機查證

| 項目 | 結論 | 依據 |
|---|---|---|
| quickshell 0.3.2 有沒有 `Socket` | **有**（間接核實）。DMS 2.0 的 `NiriService.qml` 以 `DCommon.DSocket`（內含 `Quickshell.Io.Socket` + `SplitParser`）讀 niri 事件流，`DMSService.qml` 以同法連 DMS 後端。機上 DMS 正在 niri 下正常運作，若 `Socket` 型別不存在，這兩個服務的 QML 會載入失敗。上游 v0.3.2 `CMakeLists.txt` 第 57 行 `boption(SOCKETS "Unix Sockets" ON)` | DMS 原始碼；quickshell v0.3.2 原始碼 |
| 誰當伺服器 | 匯流（Python）當伺服器，quickshell 只當客戶端，與 DMS 連 niri 的模式完全相同。不需要 `SocketServer`。`DSocket` 已處理「Socket 連線失敗後無法重撥」的 quickshell 限制（每次重連換新實例）與指數退避，可直接沿用 | `dux-qml-common` 的 `DSocket.qml` 註解 |
| DMS 修補分支 | `local/force-touch` 仍停在 v2.0.0（`9b41329`），沒有任何修補 | `git log v2.0.0..HEAD` 為空 |
| Dock 的輸入路徑 | `DockAppButton.qml` 一個 `MouseArea`：左鍵 `onPressed` 只在釘選 App 上記錄拖曳起點並啟動 500 ms 長按計時；`onReleased` 呼叫 `handleLeftClick()`；右鍵在 `onClicked` 呼叫 `showContextMenu()` | DMS v2.0.0 原始碼 |
| Dock 右鍵選單 | 已列出該 App 的所有視窗（`DockContextMenu.qml` 取 `appData.allWindows`），可點選切換或關閉 | 同上 |
| DMS 的 IPC | `DMSShellIPC.qml` 有約 30 個 `IpcHandler` target，含 `dock`；`dms ipc call <target> <fn>` 可用 | 同上 |
| Dank Lookup 的觸發 | `dank-lookup-selection` 以 `quickshell -c dank-lookup ipc call dank-lookup prepare` 取得 id，再 `capturePrepared`（DMS 前端則走 `dms ipc dank-lookup-dms`）；卡片未啟動時自動 `--daemonize` 再重試 | `bin/dank-lookup-selection` 第 227–262 行 |
| 卡片有沒有「展開」 | **沒有**。內文是一個 `Flickable`，視窗高度上限 `windowMaxHeight: 640`（`LookupTheme.qml` 第 161 行），超出即捲動；沒有截斷，也沒有逐段「更多」 | `qml/LookupCard.qml`、`qml/LookupTheme.qml` |

### 10.2 網上查證

- Quickshell 文件自 v0.1.0 起即有 `Quickshell.Io.Socket` 與 `SocketServer`（[SocketServer 文件](https://quickshell.org/docs/types/Quickshell.Io/SocketServer)）。
- 沒有找到任何 Linux shell（quickshell、DMS、其他）消費觸控板壓力事件的先例。LinuxMagicForce、Magic-TP-extended 等都在驅動或 uinput 層自行合成點擊（見 survey 第六節），不向 GUI 廣播階段。最接近的先例反而是 DMS 自己讀 niri 事件流的做法，本設計就是照抄這個模式。
- 結論：沒有現成方案可借，但所需的每一塊（Python 端 socket、quickshell 端 `Socket`、DMS 的 `DSocket` 封裝）都已存在並在機上運作。

### 10.3 三項的最小第一步

**一、匯流。** 可行，無阻擋。第一步即第六節的第一階段：以 PR #8 的 `dank-force-press-watch` 為底，加 `selectors` 事件迴圈與 `$XDG_RUNTIME_DIR/dank-force/bus.sock`，只廣播 `press`／`deep`／`release`／`cancel`，**沒有消費者**，後備指令照舊執行 `dank-lookup-selection`；附 `--monitor`。驗收：日常使用與現在無差別，`--monitor` 看得到事件。單元名稱沿用 `dank-force-press`。

**二、卡片內第二次深壓展開。** 可行，而且**不必等匯流**。先以現有 IPC 做原型：

1. `LookupController` 加 IPC 函式 `deepPress()`：卡片可見且游標在卡片上（卡片加 `HoverHandler`）時，進入展開狀態並回傳 `expanded`；否則回傳 `pass`。
2. `dank-lookup-selection` 收到來源 `force-press` 時先呼叫 `deepPress`；回傳 `expanded` 即結束，不做新查詢；否則照舊查詢。監聽器不用改，觸覺照舊由監聽器發出。
3. 代價：每次深壓多一次 `qs ipc` 往返（未量測，估計數十毫秒，發生在放開之後，不影響觸覺時機）。日後匯流第二階段再改為 `claimant` 認領。

待 Nocehi 決定「展開」的意思：現況沒有截斷，展開只能是**加高卡片**（例如上限由 640 提高到螢幕高度約 85%）；若要 macOS 那樣逐段「更多」，須先做逐段摺疊，屬卡片改版的一部分。已知副作用：深壓本身會先產生一次韌體點擊，若落在卡片的按鈕上，按鈕也會被觸發；原型階段在內文區深壓即可避開。

**三、DMS Dock 接入。** 可行，但**必須等匯流**，不宜用 IPC 原型。原因是時序：深壓在按住約 80 ms 後才判定，而 Dock 在放開時就執行點擊；實測深壓的 ≥160 持續時間最短約 122 ms，判定到放開可能只隔約 40 ms，`dms ipc` 每次起一個程序，趕不上放開，只能延遲所有 Dock 點擊或漏判，兩者都不可接受。匯流的推送（亞毫秒）加上 `awaitRelease` 才能乾淨處理。

因此 Dock 的第一步不是寫程式，而是**開始累積第三階段門檻數據**。現行監聽器已記錄 `retry=1`，可直接統計：

```sh
journalctl --user -u dank-force-press | grep -c ': click->force'   # 深壓次數（FIRE 與 COOLDOWN）
journalctl --user -u dank-force-press | grep -c 'retry=1'          # 深壓後重試次數
```

門檻：深壓 ≥ 100 次且重試率 ≤ 2%。動作建議：Dock 深壓 = 打開現有右鍵選單（已含視窗列表），不做 App Exposé，符合先前決定。修補要點：深壓時停掉 `longPressTimer`，否則釘選 App 上超過 500 ms 的深壓會進入拖曳重排；`onReleased` 對已認領的深壓不呼叫 `handleLeftClick()`。

### 10.4 建議順序

1. 立即：卡片 `deepPress()` 原型（不依賴其他兩項），同時開始統計門檻數據。
2. 接着：匯流第一階段（無消費者）。
3. 門檻達標後：DMS `ForceTouchService` + Dock 右鍵選單。
