# Magic Trackpad 深壓查詢與觸覺回饋

2026-11-06 整理。對象：Apple Magic Trackpad（A3120，USB-C，`05ac:0324`），經 USB 連接；CachyOS，核心 7.3.0-5-cachyos，niri 26.04，libinput 1.32.0。

本文記錄三部分：(1) 我們做過的測試與量得的數據；(2) 社區與上游的現成方案；(3) 觸覺回饋與深壓查詢的現況、已安裝內容及待辦事項。

---

## 一、測試與量測

### 1.1 裝置與事件來源

上游 `hid-magicmouse` 對 0x0324 逐指回報 `ABS_MT_PRESSURE`（0–253），按下時只有一個普通的 `BTN_LEFT`。驅動沒有獨立的 force click 第二段事件。libinput 不把觸控板壓力交給 compositor，所以 niri、DMS、quickshell 都拿不到壓力值。唯一可行的路線是另寫一個 evdev 監聽程式，直接讀取原始輸入（不 grab 裝置，游標與點擊照常運作）。

唯讀檢查結果：

| 項目 | 結果 |
|---|---|
| 連線 | USB（bustype 0x3） |
| 觸控節點 | `/dev/input/event16`，`'Apple Inc. Magic Trackpad'`，`ABS_MT_PRESSURE` 與 `ABS_PRESSURE` 範圍 0–253 |
| 權限 | `root:input 660`；使用者已在 `input` 群組，讀取不需 sudo |
| 另一節點 | `event15`（USB 第一個介面）一開啟即回 EPIPE，核心日誌 `unable to request touch data (-32)` 亦源於此。已知現象，可忽略，腳本會略過 |

### 1.2 壓力取樣（`pressure_probe.py`）

腳本以唯讀方式記錄每次接觸的最高壓力、觸發點擊時的壓力和按住期間的最高壓力。一輪 31 次，單指：

| 動作 | 次數 | 壓力（0–253） |
|---|---|---|
| 輕放不按 | 9 | 最高 11–51（中位數 39） |
| 觸發點擊的一刻 | 22 | 63–77（中位數 66.5） |
| 一般點擊的最高值 | 10 | 66–136 |
| 用力深壓的最高值 | 11 | 179–253 |

解讀：

- 每次點擊都在壓力約 65 時觸發，十分穩定，可推斷韌體判定點擊用的正是這個數值，它反映的是實際力度，不只是接觸面積。
- 一般點擊與深壓之間有 136→179 的空隙，門檻定在 160，放開門檻約 110。

### 1.3 原型試用（`force_press_watch.py`）

判定規則：按鍵按住期間，單指壓力須持續 ≥ 160 達 80 ms（2026-11-06 由 40 ms 放寬），才算深壓；放開按鍵時才執行查詢，讓拖曳選取先完成；觸發後冷卻 400 ms。程式只記錄壓力數值與時間，不讀取或記錄任何文字。

`--dry-run -v` 一輪的結果：

| 項目 | 結果 |
|---|---|
| 一般點擊 | 20 次全部未觸發，最高壓力 68–148 |
| 瞬間高點 | 一次一般點擊瞬間到 161，未維持 40 ms，被正確濾掉 |
| 深壓 | 10 次全部觸發，最高壓力 162–214 |
| 點擊到判定深壓 | 76–277 ms |
| 位置 | 四邊與中央表現一致 |

實際觸發三次（`FIRE`），點擊到深壓 75–606 ms，峰值 217–253。在 ghostty 與 Claude desktop 中都能彈出查詢卡片。

注意：第二輪的安全空間比第一輪窄。一般點擊最高到 148，深壓最低 162，與門檻 160 只差十多。目前靠維持時間擋住瞬間高點，已由 40 ms 放寬至 80 ms：查詢在放開時才執行，維持時間只延後觸覺確認，不影響卡片出現時間。

（2026-11-06 補充：其後的自然使用數據顯示真正的分界在時間軸上，見 3.3。）

### 1.4 觸覺脈衝（`haptic_pulse.py`）

觸控板經 USB 連接時，會另外暴露一個名為「Actuator」的 HID 介面。腳本會在 `/sys/class/hidraw/` 中找出報告描述含輸出報告 0x53 的節點，並寫入 14 字節報告：

```
53 01 <強度 u32 LE> 21 2B 06 01 00 16 41 13
```

預設強度 `0x00026C19`。實測：

```
actuator: /dev/hidraw3 (05ac:0324)  report: 53 01 19 6c 02 00 21 2b 06 01 00 16 41 13
sent
```

以 sudo 執行 `--fire --count 3`，觸控板震了三下，確認在 A3120（USB）上能按需觸發 Taptic 回饋。

**藍牙：已驗證可震（2026-11-06 19:57，Nocehi 實測）。** A3120 經藍牙配對後出現 `/dev/hidraw1`（`HID_ID=0005:0000004C:00000324`，`HID_NAME=Magic Trackpad`，不是「…USB-C」）。其報告描述只宣告 `in 0x02`（7 B）、`feature 0x55`（64 B）、`in 0x90`，沒有 `0xF2` 也沒有 `0x53`，但照外部專案的做法直接寫入 `F2 53 01 30 78 02 10 24 30 06 01 10 18 48 12`（sudo，`--count 3`），觸控板有震，且聽得見聲音。我們 USB 用的 14 字節 `53 01 …` 經藍牙寫入也沒有報錯，但**不會震**（藍牙寫入不報錯不代表被接受）。結論：藍牙必須用 `0xF2` 包住的格式；描述裏沒有宣告這個報告，只能按傳輸方式決定格式。現行 udev 規則沒有涵蓋藍牙節點（實測 `writable=no`），現行監聽程式也只找 `05ac`，所以深壓在藍牙下仍不會震，兩者待改。新版腳本 `bt-haptics/haptic_pulse.py` 按產品 ID 配對，同時認 USB 廠商 ID `05ac` 與藍牙廠商 ID `004c`，並支援藍牙的 `0xF2` 報告格式；步驟見 `bt-haptics/bt-haptics-test.md`。 新版腳本 `tools/force-touch/haptic_pulse.py` 按產品 ID 配對，同時認 USB 廠商 ID `05ac` 與藍牙廠商 ID `004c`，並支援藍牙的 `0xF2` 報告格式；步驟見 [`bluetooth-haptics-test.md`](bluetooth-haptics-test.md)。

---

## 二、現成方案與參考資料

### 2.1 觸覺回饋

| 來源 | 內容 | 對我們的意義 |
|---|---|---|
| [AppleMagicTouchstreamLP](https://github.com/disarmyouwitha/AppleMagicTouchstreamLP/blob/HEAD/windows_linux/AppleMagicTouchstream.Linux/Platform/README.md) | 逆向得出 Actuator 介面的 0x53 輸出報告，並在 Linux 上以 0x0324 實測可用 | 我們的報告格式與預設強度即取自此處 |
| [Fressl 核心補丁 v2](https://patchew.org/linux/20260514171524.110189-1-christian@fressl.at/)（2026-05-14；[v1 LKML](https://lkml.iu.edu/hypermail/linux/kernel/2605.1/13417.html)） | `haptic_click` 模組參數：0＝不變、1＝silent-low、2＝關閉。以 feature report `0x22`（點擊）與 `0x23`（放開）設定持久的韌體點擊手感；`0x53` 只用來辨認接受這些報告的介面。程式只走 USB 路徑（`is_usb_magictrackpad2()`），沒有藍牙分支。Jiri Kosina 問為何要 `READ_ONCE()`，頁面上沒有作者回覆，也沒有「已套用」記錄（2026-11-06 經 patchew 頁面核對；lore 與 hid.git 未查） | 核心只打算讓人調韌體點擊手感，不打算開放自訂觸覺；用戶態方案在可見將來仍有理由存在 |
| [Magic-TP-extended](https://github.com/knespii/Magic-TP-extended)（Python，2026-10） | 不接管點擊；按鍵已按下且壓力過門檻時，經 hidraw 寫 15 字節報告 `F2 53 01 <強度> 78 02 <sharp> 24 30 06 01 <deep> 18 48 12` 多震一下；回落後播較弱的放開脈衝並重新武裝 | **藍牙可震的直接證據**：README 標明只在 `004C:0265`（藍牙）測過，`0324` 藍牙／USB 皆「untested」。做法與我們幾乎相同；報告是 `0xF2` 包住與我們 USB 版同構的 `53 01 …` 內容 |
| [LinuxMagicForce](https://github.com/Eriskii/LinuxMagicForce)（Rust，2026-09） | 定位為藍牙版 Magic Trackpad 2 觸覺守護程式：以 `F2 21 01` 切到 host-click 模式，自己判定點擊與 force click，經 uinput 輸出按鍵，並送 `0xF2` 觸覺報告；退出時恢復韌體點擊。需 root | 第二個藍牙證據，但程式的自動偵測只認藍牙 `0265`；USB-C 版只走 USB 路徑。對 `0324` 藍牙不構成證據（2026-11-06 讀原始碼 `auto_device()`） |
| [linux-trackpad-driver](https://github.com/aaronroquefonseca/linux-trackpad-driver)（Rust，v0.1.4，2026-11-04） | 完整用戶態驅動：host-click、壓力點擊、第二層 force（「Second-level arming, hysteresis」），列出 `004c:0324` 藍牙 ID | README 稱藍牙「implemented but not hardware-verified」，CHANGELOG 0.1.4 則稱「Bluetooth that works」，兩者矛盾，不當證據。其安裝說明指出藍牙 hidraw 沒有 USB 祖先，udev 須用 `KERNELS=="0005:004C:0324.*"` 配對（見 3.3） |
| [linux-apple-trackpad](https://github.com/benjaminwestern/linux-apple-trackpad)（DKMS） | 強度檔位、110 Hz 回報率；正式的 `FF_HAPTIC` 介面仍在規劃 | 將來若有核心級觸覺 API，可改用它取代 hidraw |
| libinput、niri | 沒有觸覺 API | 只能在用戶態直接寫 hidraw |

~~限制：藍牙連接時，Linux 看不到 Actuator 介面，無法觸發觸覺。~~ 2026-11-06 更正：這是我們只找 `05ac` 與輸出報告 `0x53` 造成的推論，不是實測。外部專案已在 `0265` 經藍牙觸發觸覺（報告 ID `0xF2`），A3120 經藍牙待測，見 1.4。

### 2.2 壓力與 Force Click 行為

Apple 的設計詳見 [apple-force-touch-survey.md](apple-force-touch-survey.md)。要點：

- Apple 把 Force Click 的主要語義定為「查看、預覽」（查詢辭典、Quick Look、資料偵測），次要語義是連續變速。Dank Lookup 的用法正好落在主要語義上。
- AppKit 只定義兩個壓力階段（stage 1 普通點擊、stage 2 深壓）。`primaryDeepClick` 一旦開始拖曳就停用 stage 2。
- 觸覺樣式只有三種：`alignment`、`levelChange`、`generic`。HIG 要求觸覺與 Force 觸發都可關閉，且 Force 不應是唯一入口。

我們的硬體對照：

| Apple 概念 | 本機做法 |
|---|---|
| stage 1 | 韌體點擊（壓力約 63–77），其觸覺由韌體自行產生，無法逐次控制 |
| stage 2 | 監聽器以 ≥ 160、持續 80 ms 模擬；分辨靠維持時間（普通點擊 ≤ 44 ms，深壓 ≥ 265 ms），只容得下這一個額外階段 |
| stageTransition | 可自行計算 (p − 110) / (160 − 110)，作預熱或動畫用 |
| 拖曳時停用 stage 2 | 可從 `ABS_MT_POSITION_X/Y` 判斷位移，尚未實作 |
| 觸覺樣式 | 以「強度 × 次數 × 間隔」自訂，三種都能模擬；限 USB |
| 游標下的內容 | 無法得知。只能讀 PRIMARY 選取，所以必須先選字再深壓 |

外部壓力參考（[Magic-TP-extended](https://github.com/knespii/Magic-TP-extended) README，藍牙 `0265`）：普通點擊峰值約 64–77，刻意重按 117–253；預設門檻 140，回落到 120 以下（門檻減滯後 20）才重新武裝。我們的 160 落在其重按區間的中下段，與我們自己量的數據一致。但它只看峰值，我們的自然使用數據顯示普通點擊峰值可達 171–198，所以我們靠 80 ms 維持時間分辨（見 3.3），不能直接照搬 140。[LinuxMagicForce](https://github.com/Eriskii/LinuxMagicForce) 的預設值為普通 70、force 165、force 重新武裝 125。

### 2.2.1 接管點擊與否的取捨

- **LinuxMagicForce 接管**（host-click 模式，需 root）：因為點擊由它自己發出，它能做我們做不到的事。上次點擊放開後 350 ms 內的第二次單指按壓，force 門檻改用 220（預設 165），讓雙擊拖曳不易誤觸 force；拖曳中改用較低的放開門檻（30），避免拖到一半鬆脫。
- **Magic-TP-extended 不接管**：README 明言不切 host-click，守護程式停掉時點擊仍然正常。
- **我們選了不接管**：監聽器不 grab、不需 root，服務掛掉也不影響點擊。代價是韌體已經發出的那一下點擊無法取消，深壓只能在旁觀察，再用觸覺與「放開時才動作」去緩和。dogfood 階段維持不接管；雙擊時窗提高門檻的做法不需接管也能做（只影響我們何時判為深壓），可記為誤觸改善的候選。

### 2.3 專案內早期路線

`b9fb650:notes/gesture-trigger-exploration.md` 的方案 C 把三指長按當作 Force Click 的代替品。v0.7.1 封存觸控板路線，是因為 libinput 的 tap 判斷會誤觸。現行做法直接讀 evdev 的按鍵與壓力，不經 libinput 的 tap 判斷，不會再遇到那個問題。

---

## 三、現況

### 3.1 已安裝內容（2026-11-06，經 Nocehi 明確授權）

| 檔案 | 作用 |
|---|---|
| `~/.local/bin/dank-force-press-watch` | 監聽程式：深壓達門檻時發一次觸覺脈衝，放開時呼叫 `~/.local/bin/dank-lookup-selection`（與 Mod+Alt+D 同一指令） |
| `~/.config/systemd/user/dank-force-press.service` | systemd user 服務，隨 `graphical-session.target` 自動啟動，失敗時 2 秒後重啟 |
| `/etc/udev/rules.d/70-dank-magic-trackpad-haptic.rules` | 一行 uaccess 規則，讓登入使用者可寫入 0324 的 hidraw 節點。須由 Nocehi 以 sudo 安裝；Nocehi 其後回報手感良好，推定已安裝，但我們未在機上核實 |

udev 規則內容：

```
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="05ac", ATTRS{idProduct}=="0324", TAG+="uaccess"
```

目前參數：門檻 160、維持 80 ms、冷卻 400 ms、觸覺強度 `0x00026C19`。repo 內的對應檔案：`bin/dank-force-press-watch`、`systemd/dank-force-press.service`、`packaging/udev/70-dank-magic-trackpad-haptic.rules`；安裝方法見 [`../force-touch.md`](../force-touch.md)。

確認狀態：

```
systemctl --user status dank-force-press --no-pager
journalctl --user -u dank-force-press -n 5 --no-pager   # 應見 "haptic on /dev/hidraw3"
```

日誌若顯示 `haptic unavailable (/dev/hidraw3 not writable)`，表示 udev 規則未生效，拔插 USB 線一次即可。

### 3.2 調整與還原

- 一般點擊誤觸：在服務的 `ExecStart` 加 `--force 175`。
- 常常壓不出：降低 `--force`。
- 觸覺太強或太弱：加 `--strength <值>`；不要觸覺：加 `--no-haptic`。
- 修改後執行 `systemctl --user daemon-reload && systemctl --user restart dank-force-press`。

完全還原：

```
systemctl --user disable --now dank-force-press
rm ~/.local/bin/dank-force-press-watch ~/.config/systemd/user/dank-force-press.service
systemctl --user daemon-reload
sudo rm /etc/udev/rules.d/70-dank-magic-trackpad-haptic.rules && sudo udevadm control --reload
```

### 3.3 已知限制

- **裕度在時間軸，不在壓力軸**：自然使用數據（`margin-probe/`，34 次，其中深壓 5 次）顯示，普通點擊壓力 ≥ 160 的最長連續時間 ≤ 44 ms，深壓則持續 265–818 ms；維持時間 80 ms 正落在兩者之間。壓力門檻 160 只是入口條件：普通點擊的峰值可達 171，`area-probe/` 中甚至有 198，壓力上本來就沒有乾淨的分界，靠的是「能否在 160 以上停留 80 ms」。深壓樣本仍少，第一階段日誌（含設計稿 6.1 的重試率）會再確認。多級壓力與以時間做第二級均不可行，見設計稿 9.1。
- **觸覺：USB 與藍牙都已驗證可震（藍牙 2026-11-06，見 1.4），但安裝的元件只支援 USB**：藍牙連接時查詢照常運作。現行監聽程式只在 `05ac` 底下找觸覺節點，所以藍牙下不會震；udev 規則用 `ATTRS{idVendor}=="05ac"`，藍牙 hidraw 沒有 USB 祖先，預期不會套用（須加 `KERNELS=="0005:004C:0324.*"`）。（藍牙實測已證實 `writable=no`。）repo 已改好（2026-11-06）：規則加了藍牙行，監聽程式依觸控裝置的傳輸方式找節點，藍牙時送 `0xF2` 格式（`--bt-strength`，預設 `0x30`）；待機上安裝試用。
- **樣本有限**：只有單指、USB、兩輪樣本。換手指、斜按、改用藍牙時數值可能不同。
- **須先選字**：Linux 點擊不會自動選字，未選字時深壓查不到東西。
- **實驗性質**：監聽程式、服務與 udev 規則已納入 repo（2026-11-06），但預設安裝程式 `packaging/install-user.sh` 不會安裝或啟用它們，須另行執行 `packaging/install-force-touch.sh`。

### 3.4 路線圖（2026-11-06 依 Nocehi 決定改訂）

方向：監聽器升級為 force 事件匯流，只負責判定深壓、發觸覺、廣播事件；Dank Lookup 與 DMS 元件都是消費者。設計見 [`force-event-bus-design.md`](force-event-bus-design.md)。

1. **匯流第一階段**（連同深壓路徑只讀 PRIMARY、不再後備讀剪貼簿，見設計稿 9.5）：以 unix socket 廣播事件，暫無消費者時照舊執行 `dank-lookup-selection`，行為與現在相同；同時加入**拖曳時不觸發**（位移超過約 3 mm 即取消，比照 Apple `primaryDeepClick`）。判定改為只看壓力最高的觸點，忽略靜止且低壓的擱置手指（打字時拇指擱在下緣）；日誌記錄每次按壓的觸點數與 `progress` 越過 0.6 的次數。
2. **資料偵測**（Dank Lookup，與硬件無關，可與第 1 項平行）：日期、時區、單位換算、網址拆解與複製，全在本機完成；快捷鍵觸發一樣適用。
3. **Dank Lookup 成為匯流消費者**：以 `default` 角色接手後備查詢，並利用 `progress` 事件**接近門檻時預先查詢**。試用一兩天後，以誤觸背景查詢的比例與實際省下的延遲決定去留。
4. **測試其他程式**：Firefox 與一個 Qt 程式，確認深壓的第一段點擊後 primary selection 仍然保留。
5. **DMS Dock 深壓**（**前提：第一階段日誌顯示深壓後重試率 ≤ 2%，且至少 100 次深壓**；重試＝深壓放開後 1 秒內、游標未移動逾 2 mm 的普通點擊，詳見設計稿 6.1）：DMS 新增 `ForceTouchService`，Dock 認領深壓並打開該 App 的視窗列表。DMS 2.0.0 的 Dock 已在放開時才啟動 App；改為按下時認領，放開時等匯流的 `release`（最多 80 ms）再決定是否執行點擊，避免雙重觸發；修補走本機還是上游 PR 待定。
6. **其他 DMS 元件**：啟動器、通知、剪貼簿歷史、狀態列小工具依次接入。
7. **納入 repo**：匯流、服務與 udev 規則放進 repo，並加入設定項讓深壓、觸覺、預先查詢都可關閉。

已驗證並放棄：Firm Press（接觸面積與壓力併用）。「Verify contact area for deep press」的 90 次樣本顯示面積無助分辨（`area-probe/`）。同一批樣本中部分普通點擊峰值達 198，門檻裕度須以第一階段日誌重新確認。

可選：未選字時查游標下的字（深壓時注入一次額外點擊湊成雙擊選字），宜做成選項並先試用。

評估後不做（設計稿第九節）：多級壓力與多級觸覺（壓力軸到不了第三級；時間軸會與 265–818 ms 的自然深壓及 Dock 500 ms 長按相撞）；三指 Force（三指點擊在 libinput clickfinger 下是中鍵，會貼上 PRIMARY）。改做「降低關窗成本」（2026-11-06 Nocehi 同意）：卡片開啟時再深壓展開完整條目，以及可選的失焦自動關閉，排在第 3 項之後。

已放棄：niri 欄寬吸附觸覺。欄寬與工作區切換已交給鍵盤，觸覺落在沒在動的手下面。

---

延伸閱讀：其他平台、社區專案與可搬到 niri/DMS 的點子，見 [`apple-force-touch-survey.md`](apple-force-touch-survey.md) 第五、六節。

---

## 四、參考／來源：上游內核與 ChromeOS 的相關工作

2026-11-06 核對。commit 資料取自 torvalds/linux 的 commit 訊息；「首見版本」以各 tag 原始碼是否含有相關代碼判定（例如 v4.19 無、v4.20 有）。郵件列表資料經網頁取得，無法直接存取 lore 的部分已註明。

### 4.1 Magic Trackpad 2 驅動支援（hid-magicmouse）

| commit | 作者／日期 | 內容 | 首見版本 |
|---|---|---|---|
| [`9d7b18668956`](https://github.com/torvalds/linux/commit/9d7b18668956c411a422d04c712994c5fdb23a4b) | Sean O'Brien（seobrien@chromium.org），2018-10-02 | 新增 Magic Trackpad 2 的 USB 與藍牙支援（0x0265），切換到多點觸控模式 | 4.20 |
| [`4fb125192563`](https://github.com/torvalds/linux/commit/4fb125192563) | José Expósito，2021-05 | 修正拔除 Magic Trackpad 2 時當機 | 5.13 |
| [`d4b9f10a0eb6`](https://github.com/torvalds/linux/commit/d4b9f10a0eb6)、[`9d60648c607a`](https://github.com/torvalds/linux/commit/9d60648c607a) | José Expósito，2021-07 | 高解析度捲動（Magic Mouse） | 5.15 |
| [`0b91b4e4dae6`](https://github.com/torvalds/linux/commit/0b91b4e4dae6) | José Expósito，2021-11 | 經 USB 回報電量 | 5.17 |
| [`0aa45fcc42d8`](https://github.com/torvalds/linux/commit/0aa45fcc42d8)、[`5768701edcb7`](https://github.com/torvalds/linux/commit/5768701edcb7) | José Expósito，2021-12 | 裝置名稱被 Mac 個人化時改設固定名稱；新增「Magic Trackpad 2021」名稱 | 5.17 |
| [`87a2f10395c8`](https://github.com/torvalds/linux/commit/87a2f10395c8) | Callahan Kovacs，2024-11 | **USB-C 版（0x0324，即 A3120）支援** | 6.13 |
| [`86b05bb2d4a1`](https://github.com/torvalds/linux/commit/86b05bb2d4a1) | Qingyan Li，2024-12 | USB-C 版經藍牙連線時的裝置名稱 | 6.14 |
| [`a1556b48efc1`](https://github.com/torvalds/linux/commit/a1556b48efc1)、[`4253fe22b137`](https://github.com/torvalds/linux/commit/4253fe22b137) 等 | 多人，2026-07 | 藍牙 USB-C 版電量回報；reset-resume 後重新啟用多點觸控 | 2026-08 合入 |

要點：

- **9d7b18668956 的簽名鏈**依次為 Claudio Mettler、Marek Wyborski、Sean O'Brien，再經 Benjamin Tissoires 審閱、Jiri Kosina 合入。可見 O'Brien 是把社群既有的樹外驅動整理後送進主線，並非從零寫起。補丁約在 2018 年 9 月下旬貼上郵件列表（[LKML 1809.2/05203](https://lkml.iu.edu/hypermail/linux/kernel/1809.2/05203.html)，經 [OMG! Ubuntu 2018-09-30](https://omgubuntu.co.uk/2018/09/google-working-on-apple-magic-trackpad-2-linux-support) 轉引）；commit 作者日期為 10 月 2 日。
- **藍牙與 USB 用不同的 report ID**：藍牙 `0x31`、USB `0x02`，在 `magicmouse_raw_event()` 分兩個 case 解析。兩者最終都輸出 `ABS_MT_PRESSURE`（0–253），但解析路徑不同，藍牙下的壓力數值不能假定與 USB 相同，門檻需要重測。
- **裝置名稱**：2018 年原版確實固定設為 `"Apple Inc. Magic Trackpad 2"`，註解寫明目的是讓藍牙與 USB 套用同一套設定。**但現行主線已不是這樣**：藍牙連線時依版本設為 `Apple Inc. Magic Trackpad`、`…Trackpad 2021` 或 `…Trackpad USB-C`；USB 連線時則直接沿用 USB 描述符的名稱（`hdev->name`）。本機 A3120 經 USB 實測名稱為 `Apple Inc. Magic Trackpad`（見 1.1），經藍牙則按代碼應為 `Apple Inc. Magic Trackpad USB-C`，兩者不同，**按名稱找裝置不是穩定錨點**。現行 watcher 按產品 ID（`0x0265`、`0x0324`）比對，這反而是較穩的做法；轉用藍牙時要留意藍牙的 vendor ID 是 `0x004c` 而非 `0x05ac`。
- **驅動只回報壓力與單一 `BTN_LEFT`**，沒有任何第二段（force click）事件，也沒有觸覺輸出。這是我們自行判定深壓的原因。

### 4.2 HID 觸覺觸控板框架（Angela Czubak／Jonathan Denose）

- **第一版**：2021-12-21，Angela Czubak（acz@semihalf.com），[「Implement simple haptic HID support」，18 個補丁](https://patchwork.kernel.org/project/linux-input/cover/20211221191743.1893185-1-acz@semihalf.com/)。對象是 HID Usage Tables 的 Haptics 頁（`HID_UP_HAPTIC 0x0e`，Simple Haptic Controller）。當時未合入。
- **合入版**：2025 年由 Jonathan Denose（Google）重新提交，[「HID: Implement haptic touchpad support」](https://patchew.org/linux/20250804-support-forcepads-v2-0-138ca980261d@google.com/)，v2 於 2025-08-04、v3 於 08-18。各補丁作者為 Angela Czubak（aczubak@google.com），Denose 為 Co-developed-by。封面信指明多點觸控報告須遵循 Microsoft Precision Touchpad 規格。合入 **6.18**，主要 commit 包括 [`08a72a220e96`](https://github.com/torvalds/linux/commit/08a72a220e96)（`FF_HAPTIC` 效果類型）、[`7075ae4ac9db`](https://github.com/torvalds/linux/commit/7075ae4ac9db)（`INPUT_PROP_HAPTIC_TOUCHPAD`）、[`b44779d44f71`](https://github.com/torvalds/linux/commit/b44779d44f71)（`hid_haptic_device`）、[`8d0bf7908b5a`](https://github.com/torvalds/linux/commit/8d0bf7908b5a)（接入 hid-multitouch）。
- **6.18 正式版前已改名**：Peter Hutterer 的 [`ae8966b7b5bd`](https://github.com/torvalds/linux/commit/ae8966b7b5bd)（6.18-rc6）把 `INPUT_PROP_HAPTIC_TOUCHPAD` 改為 `INPUT_PROP_PRESSUREPAD`，並擴大到所有以壓力判定點擊的觸控板。分類變成：clickpad = `BUTTONPAD`；pressure pad = `BUTTONPAD + PRESSUREPAD`；可由使用者控制觸覺 = 再加 `FF_HAPTIC`。已以 tag 核對：v6.17 兩者皆無；v6.18-rc1 至 rc5 為 `HAPTIC_TOUCHPAD`；v6.18 為 `PRESSUREPAD`。
- **與 Apple 的關係**：框架只走 hid-multitouch，認的是 HID 標準的 Simple Haptic Controller 頁。Apple 的觸覺是 Actuator 介面上的私有輸出報告 `0x53`，hid-magicmouse 不設 `PRESSUREPAD`，也沒有 `FF_HAPTIC`。所以 A3120 不會自動接上這套框架。

### 4.3 兩條觸覺路線

| 路線 | 做法 | 好處 | 代價 |
|---|---|---|---|
| A. 私有 hidraw（現行） | 用戶態程式寫 hidraw 送 `0x53`（藍牙為 `0xF2` 包住同一內容） | 已可用，手感已驗證；外部專案顯示藍牙（`0265`）也可行 | 要 udev uaccess 規則；A3120 只驗證過 USB；格式靠逆向 |
| B. 內核轉接 | 在 hid-magicmouse 為 0x0265／0x0324 宣告 `FF_HAPTIC` 與 `INPUT_PROP_PRESSUREPAD`，把 FF 播放翻譯成 `0x53` | 可提交上游；用戶態經標準 evdev FF 介面觸發，不再需要 hidraw 權限與 udev 規則；libinput 等也能據 `PRESSUREPAD` 識別裝置 | 內核開發與上游審閱；`0x53` 位於另一個 USB 介面（Actuator），驅動要跨介面配對；藍牙下要改送 `0xF2`（`0265` 已知，`0324` 待測） |

建議：短期維持 A；B 等匯流第一階段穩定後再評估，屬於可上游的獨立專案。

### 4.4 ChromeOS 方面

- **藍牙協定棧**：ChromeOS 自 122 版起逐步以 Floss 取代 BlueZ。Floss 是 ChromeOS 用於 Android Fluoride 協定棧的前端，與 BlueZ 是不同的實作（[chromeos.dev，2024-06-11](https://chromeos.dev/en/posts/androids-bluetooth-stack-fluoride-comes-to-chromeos)）。
- **Force click**：未找到 ChromeOS 對 Apple force click 做特別處理的一手資料。能確定的只有內核一層：hid-magicmouse 只提供壓力與單一按鍵，因此任何使用此驅動的系統，在沒有額外用戶態判定的情況下，都只會收到普通點擊。[Chrome Unboxed 2018-10-19](https://chromeunboxed.com/news/wireless-trackpad-chromebook-apple-magic-trackpad-2/) 的實測也只提到實體點擊手感。ChromeOS 的手勢庫是否使用壓力，**未核實**。
- **未核實**：Magic Trackpad 走傳統藍牙 HID（BR/EDR，L2CAP）還是 BLE HOGP；macOS 藍牙回報率「約 90–120 Hz」。後者沒有出處，在用 margin_probe 實測藍牙前，不應作為結論依據。USB 基準（2026-11-06 由既有 `margin-probe/frames-*.csv` 算出）：按住時相鄰幀間隔中位數 11.0 ms（約 91 Hz；第 10–90 百分位 10.7–12.0 ms，n=1169）。這是「有變化的幀」的間隔，不是韌體標稱回報率；藍牙量法見 [`bluetooth-haptics-test.md`](bluetooth-haptics-test.md) 第五節；原始數據不在 repo。

---

## 附錄：repo 內相關檔案

| 檔案 | 用途 |
|---|---|
| `bin/dank-force-press-watch` | 現行監聽程式（含觸覺與 3 mm 位移保護） |
| `systemd/dank-force-press.service` | systemd user 服務（不隨預設安裝啟用） |
| `packaging/udev/70-dank-magic-trackpad-haptic.rules` | udev uaccess 規則（須 sudo 手動安裝） |
| `packaging/install-force-touch.sh` | 選用安裝／移除程式，不執行 sudo |
| `tools/force-touch/pressure_probe.py` | 唯讀壓力取樣，輸出每次接觸的壓力與摘要 |
| `tools/force-touch/margin_probe.py` | 自然使用下的被動裕度量測（峰值與 ≥160 最長連續時間） |
| `tools/force-touch/area_probe.py` | 壓力與接觸面積取樣（Firm Press 評估，已放棄） |
| `tools/force-touch/haptic_pulse.py` | 觸覺實驗，USB／藍牙皆認（按產品 ID），預設只列出節點，`--fire` 才發送 |
| `docs/force-touch/bluetooth-haptics-test.md` | A3120 藍牙觸覺測試步驟（含 udev 與回報率量測） |
| `docs/force-touch/force-event-bus-design.md` | force 事件匯流設計：傳輸方式、事件格式、認領規則、遷移步驟、資料偵測 |
| `docs/force-touch/apple-force-touch-survey.md` | Apple Force Touch 用途與 API 調查；第五、六節為其他平台、社區專案與 niri/DMS 點子 |

量測原始數據（`margin-probe/`、`area-probe/` 的 CSV 與圖）及第一版原型 `force_press_watch.py` 未納入 repo，仍存於專案筆記資料夾。
