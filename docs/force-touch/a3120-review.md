# Force 事件匯流設計 v3：A3120 面向審閱

2026-11-06。對象：[設計稿](force-event-bus-design.md)第三版與 repo 快照 `5c99cfb`。只審閱，沒有改程式，也沒有動機上任何東西。

依據：上游 `drivers/hid/hid-magicmouse.c`（torvalds/linux master，今日取得）、`bin/dank-force-press-watch` 與本資料夾各文件、專案記憶中的 niri 觸控板設定。libinput 原始碼與 quirks 檔今日取不到（freedesktop GitLab 擋了抓取），凡涉及 libinput 內部規則的地方都標「未核實」，並附機上核實方法。

## 結論

1. **最大的問題：匯流眼中的「左鍵」不等於應用收到的按鍵。** 驅動只送一個韌體點擊 `BTN_LEFT`；右鍵、中鍵完全是 libinput clickfinger 按「手指數」在 compositor 端算出來的。設計 3.3 為了容忍擱置的拇指而放寬判定，但 libinput 若把那隻拇指算成第二指，應用收到的是**右鍵**，匯流卻照樣判深壓、走後備鏈查詢。三指深壓則是中鍵貼上。須把「邏輯按鍵」納入協定（B1）。
2. **時間戳要用核心事件時間，並設成 CLOCK_MONOTONIC。** 這樣匯流的 `t` 與 libinput（以至 Wayland 的 `wl_pointer.button` time）同一時基，Qt 與匯流按壓的配對可以從「50 ms 內先後到達」改為按時間戳比對（B2）。
3. **選取應在 `deep` 當下擷取，而不是放開後。** 第一段韌體點擊落在選取範圍內，不少程式會在放開時取消選取；查詢在放開後才讀 PRIMARY，可能讀到空的（B5）。
4. 藍牙與 USB 的觸點解碼是同一段程式，**壓力刻度應相同**；要重測的是幀率與抖動，而不是門檻（A2）。
5. 解析度兩軸不同（X 47、Y 44 單位/mm），現行程式只用 X（A1）。

其餘見下。整體方向（不接管點擊、匯流當 socket 伺服器、按下認領、放開以 `release` 為準）不受 A3120 特性推翻，只需補上 B1–B7。

## A. 驅動層事實（hid-magicmouse.c）

| # | 事實 | 程式位置 | 對設計的意義 |
|---|---|---|---|
| A1 | 0x0265 與 0x0324 共用 `TRACKPAD2_*` 常數：X −3678…3934、實體 160 mm，Y −2478…2587、實體 114.9 mm。`input_abs_set_res` 得 **X 47、Y 44 單位/mm**（整數截斷） | `TRACKPAD2_RES_X/Y` | 設計 6.1 與現行 `run()` 只取 X 的 resolution 換算兩軸，Y 方向位移約少算 6%。改為分軸換算。驅動永遠設 resolution，「resolution 為 0」的後備分支對 A3120 不會觸發，可保留作通用防呆但不必列為待辦 |
| A2 | 觸點逐點解碼都走 `magicmouse_emit_touch()`，`pressure = tdata[7]`；USB（報告 `0x02`，12 字節前綴）與藍牙（`0x31`，4 字節前綴）只有前綴不同 | `__magicmouse_raw_event()` | notes.md 4.1「藍牙壓力不能假定與 USB 相同」可以放寬：刻度相同，門檻 160 應可沿用。仍要重測的是**幀率**（USB 約 91 Hz）與**到達抖動**，因為「≥160 持續 80 ms」是時間判定。建議判定同時要求最少幀數（例如 ≥ 5 幀），以免藍牙幀稀時一兩幀就湊夠 80 ms |
| A3 | 按鍵只有 `BTN_MOUSE`（`clicks = data[1]`，韌體點擊位元）；驅動主動清掉 `BTN_RIGHT`、`BTN_MIDDLE`，設 `INPUT_PROP_BUTTONPAD` | `magicmouse_setup_input()` | 右鍵、中鍵只存在於 libinput 之後。見 B1 |
| A4 | 觸點只有在 state 為 `0x80` 時才算 down；最多 16 槽，`INPUT_MT_TRACK` 由核心分配追蹤 ID | `emit_touch()` | 手指將離開時槽位會先釋放，`touches` 在放開前一兩幀可能少一點。`release` 時的觸點數不可靠，判定應以按住期間為準（現行做法已是如此） |
| A5 | 有 `ABS_MT_TOUCH_MAJOR/MINOR`（0–1020）；沒有 `ABS_MT_TOOL_TYPE`，沒有 `INPUT_PROP_PRESSUREPAD`、`FF_HAPTIC` | 同上 | 面積對深壓無用（已驗證），但對「拇指 vs 指尖」可能仍有用（B4）。觸覺仍只能走 hidraw |

## B. A3120 特性對設計的影響

### B1. 邏輯按鍵與擱置拇指（嚴重，改協定）

機上 niri 設 `click-method "clickfinger"`，套用於所有觸控板，A3120 亦然。libinput 在按下的一刻數手指：一指左鍵、二指右鍵、三指中鍵。libinput 會排除它認定的拇指與手掌，兩指相距很遠時也可能算一指，但**具體門檻未核實**。

所以同一次實體按壓：

| 情況 | 匯流看到 | 應用看到 | 現行設計的結果 |
|---|---|---|---|
| 食指深壓，拇指擱在下緣、libinput 排除拇指 | `BTN_LEFT`，2 觸點，忽略 1 | 左鍵 | 正確 |
| 同上，但 libinput **沒有**排除拇指 | 同上 | **右鍵**（彈出右鍵選單） | 匯流判深壓，後備鏈查詢：右鍵選單加查詢卡片同時出現 |
| 三指深壓 | 3 觸點 → `cancel: multitouch`（若其他兩指壓力 ≥ 50） | **中鍵**（貼上 PRIMARY） | 不查詢，但貼上已發生，匯流無從阻止 |

6 小時樣本中已有 5 次三觸點、≥160 持續 88–2710 ms 的按壓，說明這不是假想情況。

建議：

1. `press` 加 `fingers`：匯流按 libinput 的規則**估計**的手指數，另附各觸點相對位置（mm），供校準。
2. 後備鏈（無人認領時執行查詢）只在 `fingers == 1` 時走；估計值為 2 或 3 時，`release` 帶 `deep: true` 但不走後備，日誌記 `skip=fingers`。
3. 認領者以 Qt 的實際按鍵為準：`claim` 加 `button`（`mouse.button`），匯流只接受 `LeftButton` 的認領；DMS 端只在左鍵 `onPressed` 時配對。右鍵按壓即使成為深壓也不消耗，照常彈出右鍵選單。
4. 第一階段日誌加一欄 `fingers`，並請 Nocehi 做一次對照：`libinput debug-events` 下拇指擱著、食指在不同高度按下，看 libinput 回報 `BTN_LEFT` 還是 `BTN_RIGHT`，據此校準匯流的估計規則。
5. 選項（不急）：niri 26.04 會載入 `~/.config/libinput/plugins` 的 libinput Lua 外掛。外掛可在 libinput 處理前改寫觸點，把「靜止且低壓」的拇指觸點直接拿掉，讓 libinput 與匯流用**同一條**擱置手指規則：應用收到左鍵，匯流也不用猜。代價是兩份程式要維持同一判定，且外掛會影響游標（拇指再也推不動游標）。值得在第一階段數據出來後再評估。

設計稿 9.2 第 1 點（三指點擊是中鍵）結論不變，理由可以補上：中鍵是 libinput 算出來的，匯流無法取消。

### B2. 時鐘與按壓配對（中，改實作，可能簡化協定）

- python-evdev 的事件時間預設是 CLOCK_REALTIME；libinput 會把自己的 fd 設成 CLOCK_MONOTONIC。匯流應對自己的 fd 做 `EVIOCSCLOCKID`（CLOCK_MONOTONIC），並以**事件時間戳**而不是讀到事件時的 `time.monotonic()` 計算維持時間與 `t`。好處：Python 排程延遲不再混入 80 ms 判定。
- 這樣 `press.t` 與 libinput 的事件時間同源。Wayland `wl_pointer.button` 帶毫秒時間；若 niri 原樣轉送 libinput 的時間（**未核實**，smithay 的慣例是如此），DMS 可以用時間戳精確配對，不再依賴「50 ms 內先後到達」。Qt 端：`MouseArea` 的 `MouseEvent` 似乎不公開時間戳，Pointer Handlers 的 `eventPoint.timestamp` 可能可以（**未核實**）。核實前保留 50 ms 視窗作後備。
- `t` 改為核心單調時鐘毫秒後，它就不只「供除錯」，建議在 3.1 註明。

### B3. 三指拖曳與停用滑動手勢（中，改文字）

機上以 `LD_PRELOAD` 的 enable-3fg-drag 墊片啟用了三指拖曳，並在本機修補版中丟掉所有 swipe 手勢。設計稿 9.2 第 2 點（「三指滑動是 niri 的工作區手勢」）在本機已不成立，應改為：三指移動＝三指拖曳，libinput 送出邏輯左鍵按住，但**沒有** `BTN_LEFT`。影響：

- Qt 收到 `onPressed`，匯流沒有 `press` → 未配對，照常處理。設計 4.1 已涵蓋，正確。
- 三指拖曳中再實體按下：匯流看到 3 觸點的 `BTN_LEFT`，libinput 則在拖曳狀態中。依 B1 規則（`fingers == 3` 不走後備）即可。
- 墊片是否也對 A3120 生效未核實：看 `journalctl --user -u niri -b | grep -i '3-finger'` 是否有 Magic Trackpad 一行。

### B4. 擱置手指與防打字誤觸（低，補說明）

- libinput 的 disable-while-typing（dwt/dwtp）只把**內建**鍵盤與**內建**觸控板配對，外接的 A3120 不受保護。打字時拇指擱在板上的位移會照樣推動游標。這不是匯流的問題，但意味着 3.3 的「擱置」在 compositor 端並不存在，B1 的不一致會比在內建觸控板上更常見。
- 判定拇指可以加兩個 A3120 有的維度：離下緣的距離（mm，需 A1 的分軸換算）與 `TOUCH_MAJOR`。面積不能分辨深壓與點擊（已驗證），但拇指的接觸面通常明顯大於指尖，第一階段日誌可順便記錄被忽略觸點的 `TOUCH_MAJOR` 與 Y 位置。

### B5. 第一段點擊與 PRIMARY 選取（中，改時序）

深壓必然先有一次韌體點擊，落在選取範圍內。GTK、Firefox 等常見行為是：在選取內按下又不拖曳，放開時取消選取。PRIMARY 的擁有權是否隨之丟失因程式而異（即未決事項第 4 項）。現行流程在**放開後**才讀 PRIMARY，正好落在取消選取之後。

建議：在 `deep` 當下（按鍵仍按住，應用尚未處理放開）就讀取選取，放開時才顯示卡片。這等於把「預先查詢」裏的擷取部分變成預設行為，與預先查詢的去留無關。隱私邊界不變：只有成為深壓才讀。後備鏈（匯流自己執行 `dank-lookup-selection`）同理改為在 `deep` 時起程序擷取、放開時顯示，或保持現狀並把未決事項第 4 項的測試放在第一階段。

### B6. 觸覺節點（低，改協定欄位與實作）

- 藍牙寫入即使被裝置忽略也不報錯（notes.md 1.4 實測），所以 `hello.haptic` 的布林值只能表示「可寫」，不能表示「會震」。改為 `haptic: "usb" | "bt" | null`，文件寫明語義。
- `os.write` 寫 hidraw 是阻塞呼叫；藍牙下經 hidp 可能卡住片刻。匯流與 socket 共用一個 `selectors` 迴圈，hidraw fd 應設 `O_NONBLOCK`，寫入失敗（`EAGAIN`）就丟棄該次脈衝，不要拖住事件廣播。
- 第一段點擊的觸覺由韌體產生，不接管點擊就無法取消或調整。Fressl 的核心補丁（`haptic_click`）若合入，也只改韌體點擊手感，不影響本設計。

### B7. 換線、休眠與裝置遺失（中，改協定與實作）

- 藍牙使用中插上 USB-C 線（例如充電），觸控板會改走 USB：藍牙節點消失、出現新的 USB 節點，觸覺節點也要換。現行 `find_device()` 取第一個符合者；若兩個節點短暫並存，可能選中閒置的一個。建議同時打開所有符合的節點，以實際有事件者為準，並且觸控節點與觸覺節點**一起**按傳輸方式重選。
- 進行中的按壓遇到換線或遺失：發 `cancel: device_lost`（已有），之後另發一個 `device` 事件（`connected`、`transport`、`haptic`），讓 DMS 知道匯流仍在但裝置變了。現在只有 `hello` 帶這些資訊，重連裝置後消費者看不到。
- USB 休眠喚醒後若多點觸控模式未恢復（主線 2026-08 已修 reset-resume，7.3 核心應已包含），會出現「有 `BTN_LEFT` 而沒有觸點」的按壓。匯流應把 `touches == 0` 的按壓標成 `degraded`，不計入 6.1 的重試率與深壓統計。
- USB 第一個介面的節點開啟即 EPIPE（已知），匯流的重掃描要繼續略過它，不要每 2 秒記一次錯誤。

## C. 與 DMS、niri、Wayland 的交互

- **Wayland 隱式抓取**：按住期間指標事件都送到按下時的 surface。所以 Dock 在 `deep` 時（按鍵仍按住）打開右鍵選單，放開事件仍送給 Dock 按鈕，不會誤選選單項目。這支持 10.3 的「Dock 深壓＝打開現有右鍵選單」，而且可以比照 macOS 在深壓當下就打開。實際行為取決於 DMS 選單是 layer-shell 還是 xdg_popup（popup 會建立自己的抓取），上 Dock 前要實測一次。
- **按下認領的前提成立**：A3120 的第一段點擊在壓力約 65 時就觸發，比深壓早至少 75 ms 加 80 ms，所以 4.1「按下時認領」的時間預算不受 A3120 特性影響。
- **右鍵也會來自 A3120**：DMS 元件若同時處理左右鍵（如 Dock 的 `onClicked` 右鍵選單），務必只在左鍵配對（B1 第 3 點）。
- **其他 shell**：協定與 A3120 無關，任何 quickshell shell 都能用同一個 `Socket` 接入；非 quickshell 的 bar（waybar 之類）也只需要一個 socket 客戶端。這一點不影響目前的設計選擇。
- **接管點擊（LinuxMagicForce 的 host-click 模式）**：這是唯一能真正「吞掉」第一段點擊的路線，可一併解決 B5（取消選取）、卡片按鈕被誤按（10.3）與 Dock 的等待邏輯。代價是需要 root 與 uinput，匯流當掉時點擊可能失效，而且 A3120 經 USB、藍牙的 host-click 都沒有人驗證過。建議維持不接管，把它記為「若 B5 或卡片按鈕問題無法解決時」的後路。

## D. 建議修改清單（對應設計稿章節）

| 章節 | 修改 |
|---|---|
| 3.1 `hello` | `haptic` 改為 `"usb" \| "bt" \| null`；加 `res: [47, 44]` |
| 3.1 `press` | 加 `fingers`（libinput 規則估計值）；`t` 改為核心單調時鐘毫秒 |
| 3.1 新事件 | `device`（`connected`、`transport`、`haptic`）；`release` 加 `skip`（`fingers`、`degraded`） |
| 3.2 `claim` | 加 `button`，只接受左鍵 |
| 3.3 | 判定加最少幀數；拇指判定可加 Y 位置與 `TOUCH_MAJOR`；說明 dwt 不涵蓋外接觸控板；加 libinput 對照校準 |
| 4.3 第 3 點 | 後備鏈只在 `fingers == 1` 時執行 |
| 5.1 | `EVIOCSCLOCKID` 與事件時間戳；分軸 resolution；hidraw `O_NONBLOCK`；多節點監看與換線處理；`degraded` 標記 |
| 5.2 / 6 | 選取在 `deep` 時擷取、放開時顯示（B5） |
| 6.1 第 3 點 | 分軸換算；刪去「resolution 為 0」的說明或改為通用防呆 |
| 8 | 新增未決：libinput 拇指與距離規則（B1）；niri 是否轉送 libinput 時間（B2）；墊片是否作用於 A3120（B3）；DMS 選單 surface 類型（C） |
| 9.2 第 2 點 | 改為三指拖曳與已停用的滑動手勢（B3） |
| notes.md 4.1 | 藍牙壓力刻度應同 USB，重測對象是幀率與抖動（A2） |

## E. 機上核實（需要時由 Nocehi 執行，皆唯讀）

```sh
# A3120 的 libinput quirks 與屬性（N 為觸控節點）
libinput quirks list /dev/input/eventN
cat /sys/class/input/eventN/device/properties
# B1：拇指擱在下緣，食指在上、中、下三個高度各按一次，看 BTN_LEFT 還是 BTN_RIGHT
libinput debug-events --device /dev/input/eventN | grep -E 'BUTTON|POINTER_BUTTON'
# B3：三指拖曳墊片是否作用於 A3120
journalctl --user -u niri -b | grep -i '3-finger'
```
