# Apple Force Touch 用途調查與 Linux 對照

2026-11-06。僅調查，未改動任何程式碼。

## 一、Apple 系統內建的 Force Click 行為

來源：[How to force click and get haptic feedback on Apple trackpads](https://support.apple.com/en-us/102309)、HIG [Pointing devices](https://developer.apple.com/design/human-interface-guidelines/pointing-devices)。

| 行為 | Apple 原文要點 |
|---|---|
| 查詢（Look up） | 對網頁、郵件、文件中的文字 Force Click，彈出辭典、Wikipedia 等資訊。替代手勢：三指輕點，或 Control-click → Look Up。 |
| 資料偵測（data detectors） | 日期、地址、地點、電話、行事曆事件、航班號、包裹追蹤號、URL 等，Force Click 彈出預覽 popover。 |
| Quick Look | Finder 中對檔案 Force Click 預覽內容。 |
| 重新命名 | Finder 中對檔名 Force Click 進入編輯。 |
| App Exposé | Dock 上對 App Force Click 顯示其所有視窗。 |
| 地圖插針 | Maps 中對地點 Force Click 放置圖釘。 |
| 變速媒體控制 | 按快轉、倒轉鍵時改變壓力即改變速度（不支援時改為多次點擊）。 |
| 觸覺回饋 | 點擊時，以及在支援的 App 中把圖形、文字等拖曳至對齊時提供震動。 |

設定：系統設定 → 觸控式軌跡板 → 「Force Click 與觸覺回饋」；「查詢與資料偵測器」可選「單指 Force Click」或「三指輕點」。HIG 將 Force Click 定義為「點擊後再用力按，顯示 Quick Look 或查詢視窗；以不同壓力操作壓力敏感控制項，例如變速媒體控制」，並要求「不要重新定義系統級手勢」。

要點：Apple 把 Force Click 的**主要語義固定為「查看／預覽」**（查詢、Quick Look、資料偵測），次要語義是**連續變速**。Dank Lookup 目前的用法正好落在主語義上。

## 二、AppKit API 模型

**壓力事件** [`NSEvent.EventType.pressure`](https://developer.apple.com/documentation/appkit/nsevent/eventtype/pressure)：

- [`stage`](https://developer.apple.com/documentation/appkit/nsevent/stage)：0＝不足以構成按下；1＝普通 mouse-down；2＝超出普通點擊的額外壓力，「一般應用於啟動查詢或即時動作」。階段可跳躍（stage 2 直接放開可跳到 0）。
- [`pressure`](https://developer.apple.com/documentation/appkit/nsevent/pressure)：**每個 stage 各自 0–1**，Apple 明言不應把兩段合併成更寬的範圍；連續輸入宜只取 stage 1 壓力（最舒適），stage 2 壓力僅在少數需要先給觸覺提示的情況下使用。
- [`stageTransition`](https://developer.apple.com/documentation/appkit/nsevent/stagetransition)：接近下一階段時 0→1，接近放開時 0→−1；專供過渡動畫（例如 Safari 連結預覽逐漸浮現）。

**壓力行為** [`NSEvent.PressureBehavior`](https://developer.apple.com/documentation/appkit/nsevent/pressurebehavior-swift.enum)，經 [`NSView.pressureConfiguration`](https://developer.apple.com/documentation/appkit/nsview/pressureconfiguration) 於 mouse-down 前設定：

| 行為 | 階段 | 觸覺時機 | 用途 |
|---|---|---|---|
| primaryClick / primaryDefault | 1 | 按下、放開 | 普通點擊 |
| primaryGeneric | 1，連續壓力 | 按下、放開 | 繪圖、筆刷 |
| primaryAccelerator | 1，特殊壓力曲線 | 按下、放開 | 速度控制（[NSAcceleratorButton](https://developer.apple.com/documentation/appkit/nsacceleratorbutton)） |
| [primaryDeepClick](https://developer.apple.com/documentation/appkit/nsevent/pressurebehavior-swift.enum/primarydeepclick) | 2 | 按下、放開、進出 stage 2 | Force Click；**一旦開始拖曳即停用 stage 2** |
| [primaryDeepDrag](https://developer.apple.com/documentation/appkit/nsevent/pressurebehavior-swift.enum/primarydeepdrag) | 2 | 同上，拖曳中仍有效 | 拖曳中用力（如拖到資料夾上用力即彈開） |

[`NSMultiLevelAcceleratorButton`](https://developer.apple.com/documentation/appkit/nsmultilevelacceleratorbutton)：1–5 個離散壓力級（預設 2），每達一級給輕觸覺並發送 action。

（上表已涵蓋四種主要模式：深壓查詢模擬的是 primaryDeepClick；primaryDeepDrag、primaryAccelerator、primaryGeneric 的 Linux 對應見第六節。）

**觸覺** [`NSHapticFeedbackManager`](https://developer.apple.com/documentation/appkit/nshapticfeedbackmanager)，只有三種樣式（HIG [Playing haptics › macOS](https://developer.apple.com/design/human-interface-guidelines/playing-haptics)）：

- `alignment`：拖曳物件對齊、縮放到指定尺寸、到達起點／終點（如影片軌道頭尾）。
- `levelChange`：在離散壓力級之間移動（多級加速鈕）。
- `generic`：其他情況。

HIG 原則：按文件語義使用、一致、與視覺同步、勿濫用、偏好短促離散脈衝、**必須可關閉**。

## 三、我們的硬體能做到什麼

| Apple 概念 | Linux 現況 |
|---|---|
| stage 1 | 韌體點擊（壓力約 63–77 觸發，BTN_LEFT），本機觸覺由韌體自行產生，無法逐次控制。 |
| stage 2 | 監聽器以 ≥160、持續 40 ms 模擬；實測普通點擊峰值 ≤148、用力 ≥162，**只容得下一個額外階段**。第三級（例如 ≥225）落在用力按壓的上緣（179–253），易累且誤判，不建議。 |
| pressure（連續） | 0–253 每觸點，可做連續映射；但 stage 1 區間（66–136）用來做變速時與普通點擊重疊，須在按住後才開始計算。 |
| stageTransition | 可自算：(p−110)/(160−110)，用於預熱或動畫。 |
| 拖曳中停用 stage 2 | ABS_MT_POSITION_X/Y 可得，能比照 deepClick 判斷位移。 |
| 觸覺樣式 | 0x53 報告含 u32 強度，可用「強度 × 次數 × 間隔」自訂：單次強脈衝＝generic，弱單擊＝levelChange，兩下短脈衝＝alignment／到底。USB 與藍牙都已在 A3120 上驗證（藍牙 2026-11-06，見 notes.md 1.4）。 |
| 游標下的內容 | **無法得知**。Apple 的查詢、Quick Look、資料偵測都靠 App 回報游標下的物件；我們只能讀 PRIMARY 選取或聚焦視窗（niri msg）。 |

## 四、建議（依價值排序）

1. **拖曳時抑制 Force（比照 primaryDeepClick）**：按下後位移超過數毫米即不觸發。用力拖選文字時最易誤觸，這是低成本、立刻有感的改進。
2. **接近閾值時預先查詢（stageTransition 類比）**：壓力過 ~140 即在背景讀選取並查詢，到 160 只負責顯示，可把卡片延遲藏進按壓過程。只在本機執行，不違反隱私邊界；需注意普通重按也會觸發背景查詢（結果丟棄即可）。
3. **無選取時查游標下的字**：Apple 的核心體驗是「不必先選取」。可在 Force 時注入一次額外點擊湊成雙擊選字，再查 PRIMARY。最接近原生，但會干擾部分 App（雙擊開檔、終端機行為），宜做成選項並先行試用。
4. **卡片內的第二次 Force**：卡片開啟時再次 Force Click → 展開完整條目或切換辭典，配 levelChange 式弱脈衝。符合「Force＝看更多」的語義。
5. **到底回饋（alignment）**：卡片捲動到頂／底時給兩下短脈衝。需 quickshell → 監聽器的 IPC，價值較低。
6. **不建議**：三級以上壓力（硬體裕度不足）；把 Force 用於非查看類動作（HIG 語義不符）；變速控制（目前沒有對應的 UI 標的）。

另：依 HIG，觸覺與 Force 觸發都應可在設定中關閉；Force 不應是唯一入口（現有快捷鍵已滿足）。

---

## 五、其他平台與社區做法（2026-11-06 補充，來源：Nocehi 另一輪調查）

### 5.1 Apple 其他平台

- **iPhone 3D Touch**（6s 至 XS）：[主畫面快捷操作](https://developer.apple.com/library/archive/documentation/userexperience/conceptual/adopting3dtouchoniphone/3DTouchAPIs.html)（深壓 App 圖示，最多四項）、Peek and Pop、Live Photo 預覽、按住鍵盤變成游標觸控板。iPhone 11 起全面改為長按的 Haptic Touch。
- **Apple Watch**：Force Touch 用於叫出隱藏選單與切換錶面，[watchOS 7 將其移除](https://www.macrumors.com/2020/06/23/apple-drops-force-touch-gesture-in-watchos-7/)。一般認為原因是隱藏手勢難以發現，印證「Force 不應是唯一入口」。
- **iPhone 16 Camera Control**：壓力感應，輕按叫出控制層（類似快門半按）；但[開發者 API](https://developer.apple.com/tutorials/data/documentation/avfoundation/enhancing-your-app-experience-with-the-camera-control.md) 只開放滑桿與選擇器，不開放壓力數值。

整體趨勢：Apple 在手機與手錶上把壓力手勢收回為長按，只在 Mac 觸控板保留，且用途收斂到「預覽」與「變速」。參見 [Wikipedia: Force Touch](https://en.wikipedia.org/wiki/Force_Touch)。

### 5.2 社區專案

| 專案 | 做法 | 對 A3120 的參考價值 |
|---|---|---|
| [TrackWeight](https://github.com/KrishKrosh/TrackWeight) | 經私有 MultitouchSupport 讀壓力，當電子秤；作者稱數值即為克；須手指接觸，僅供實驗 | 低。我們的 0–253 未經砝碼校準，`hid-magicmouse` 的換算未知。[TidBITS 實測](https://talk.tidbits.com/t/appbits-trackweight-turns-your-trackpad-into-a-scale-sort-of/31609)：只有偵測到電容接觸才有壓力事件，手指幾乎不可能維持恆定壓力，失敗遠多於成功，稱之為「inspired hack」 |
| [Tactile](https://github.com/Mason363/Tactile)（macOS，MIT，2026-07，[AppleVis 介紹](https://applevis.com/comment/211639)） | 讀 Accessibility tree，游標停在可點擊元素上時震一下；可選視覺提示，對象包括低視力用戶 | 觸覺作「語義」（這裏可以按）而非「確認」（已按下）。Linux 上理論可經 AT-SPI 做，但 Wayland 下背景程式難以取得全域游標位置；按第八節分工，這類判斷應由知道 hover 狀態的 GUI 層（DMS）發起，監聽器只提供觸覺服務 |
| [LinuxMagicForce](https://github.com/Eriskii/LinuxMagicForce) | 藍牙 `0265` 觸覺守護程式；ridge haptics（手指在觸控板座標上越過設定的分隔線時震）、雙指／三指捲動紋理、拖曳按距離脈衝；雙擊時窗內提高 force 門檻 | ridge 是第六節第 1 項的前例（只作記錄）；它的分隔線是觸控板座標上固定的格線，不是 niri column 的邏輯邊界。雙擊門檻見 notes.md 2.2.1 |
| [Magic-TP-extended](https://github.com/knespii/Magic-TP-extended)、[linux-trackpad-driver](https://github.com/aaronroquefonseca/linux-trackpad-driver) | 前者不接管點擊、藍牙寫 hidraw 震；後者完整用戶態驅動，含第二層 force 與滯後 | 壓力參考值與兩層 force 可行性，見 notes.md 2.1–2.2 |
| [Inklet、Autograph](https://eclecticlight.co/2015/12/22/digging-deeper-into-force-touch-and-the-magic-trackpad-2/) | 觸控板改作絕對定位壓感繪圖板；邊緣做「虛擬滑桿」控制復原、縮放、筆刷大小 | 中。見 6.5 |
| BetterTouchTool | 可自訂點擊與深壓門檻，附即時壓力檢視 | 概念同 `pressure_probe.py`；可借鏡「門檻可調＋即時顯示」做校準工具 |
| Google Pixel 4 Firm Press | 無壓力感測器，以接觸面積推算力度 | **值得一試**：`hid-magicmouse` 亦回報 `ABS_MT_TOUCH_MAJOR/MINOR`，可與壓力併用以拉開普通點擊與深壓的距離（未驗證） |

## 六、可搬到 niri 與 DMS 的點子（附 A3120 可行性）

前提：我們只有一個額外階段（普通點擊最高約 148，深壓至少約 162）；監聽器不知道游標下是什麼；觸覺在 A3120 上 USB 與藍牙都可用（藍牙 2026-11-06 驗證），以下觸覺類點子不限於插線。排序沿用 Nocehi 調查的投入產出比，後附評估。

1. **niri 欄寬吸附時給 `alignment` 觸覺。** 經 niri IPC 事件流監聽版面變化，碰到預設欄寬即發 0x53。
   - 可行性：硬體無障礙（不需壓力階段）。疑點在 niri 端：預設欄寬多以鍵盤切換，此時觸覺落在手不在的觸控板上，沒有意義；滑鼠互動式調寬是否會吸附預設寬度、事件流是否即時回報寬度，須先查 niri 26.04 原始碼與 `niri msg event-stream`。**2026-11-06 Nocehi 決定放棄**：欄寬與工作區切換已交還鍵盤，觸覺落在沒在動的手下面。
   - 前例（僅記錄）：LinuxMagicForce 的 ridge haptics 已做「越線即震」，但線是觸控板座標上的固定格線；接 niri IPC 的邏輯邊界版本仍沒人做過。
2. **DMS Dock 深壓開啟該 App 的視窗列表（App Exposé）；啟動器圖示深壓叫出快捷操作。**
   - 可行性：中。監聽器不知游標位置，需改為「監聽器只廣播 force 事件，由 DMS 依自己的 hover 狀態決定動作」。**此點已升級為整體方向**，見 [`force-event-bus-design.md`](force-event-bus-design.md)；另查證 DMS 2.0.0 的 Dock 本已在放開時才啟動 App。需改 DMS（外掛或上游）；stage 1 先於 stage 2 的衝突，由深壓在放開前把按壓標記為「已消耗」解決。
3. **查詢支援資料偵測。** 選取若為日期、網址、追蹤號，卡片顯示相應預覽。
   - 可行性：高，與硬體無關，純 Dank Lookup 功能。但須守產品邊界（不上網）：只能做本機能完成的事，例如日期換算、網址顯示與複製、單位換算，不能抓取網頁或查物流。
4. **加速器（primaryAccelerator 思路）**：媒體快轉、捲動時壓力越大越快。
   - 可行性：中。連續數值不受單一門檻限制，但 stage 1 區間（約 66–136）與普通點擊重疊，須在按下並保持後才開始映射；且觸控板本身沒有「快轉鍵」，需要一個 UI 標的（DMS 媒體控制）配合，同第 2 項須由 DMS 判斷 hover。
5. **仿 Inklet 做壓感繪圖板。** 以 uinput 模擬帶 `ABS_PRESSURE` 的繪圖板，供 Krita 使用。
   - 可行性：中。壓力範圍堪用（輕觸 11–51 亦有讀數），但同一裝置仍經 libinput 驅動游標，須加模式切換並在繪圖模式下 grab 裝置，與現行「不 grab」設計相反；壓力曲線未校準，需在 Krita 端調整。
6. **TrackWeight 式電子秤。** 玩具性質；未校準且須手指接觸，不建議投入。

## 七、補充來源

- [Apple Support: How to use the Force Touch trackpad](https://support.apple.com/en-euro/102309)
- [NSEvent.PressureBehavior](https://developer.apple.com/tutorials/data/documentation/appkit/nsevent/pressurebehavior-swift.enum.md)
- [NSHapticFeedbackManager.FeedbackPattern](https://developer.apple.com/tutorials/data/documentation/appkit/nshapticfeedbackmanager/feedbackpattern.md)
- [3D Touch APIs (Apple archive)](https://developer.apple.com/library/archive/documentation/userexperience/conceptual/adopting3dtouchoniphone/3DTouchAPIs.html)
- [Enhancing your app experience with the Camera Control](https://developer.apple.com/tutorials/data/documentation/avfoundation/enhancing-your-app-experience-with-the-camera-control.md)
- [TrackWeight](https://github.com/KrishKrosh/TrackWeight)
- [Eclectic Light: Digging deeper into Force Touch and the Magic Trackpad 2](https://eclecticlight.co/2015/12/22/digging-deeper-into-force-touch-and-the-magic-trackpad-2/)
- [Wikipedia: Force Touch](https://en.wikipedia.org/wiki/Force_Touch)
- [Eriskii/LinuxMagicForce](https://github.com/Eriskii/LinuxMagicForce)（2026-11-06 讀 README 與 `src/main.rs`）
- [knespii/Magic-TP-extended](https://github.com/knespii/Magic-TP-extended)（2026-11-06 讀 README 與 `mtp-forced.py`）
- [linux-trackpad-driver](https://github.com/aaronroquefonseca/linux-trackpad-driver)（v0.1.4；2026-11-06 讀 README、INSTALL、CHANGELOG）
- [PATCH v2 HID: magicmouse: add haptic click configuration（patchew）](https://patchew.org/linux/20260514171524.110189-1-christian@fressl.at/)
- [Tactile（AppleVis）](https://applevis.com/comment/211639)、[Mason363/Tactile](https://github.com/Mason363/Tactile)
- [TidBITS Talk: TrackWeight](https://talk.tidbits.com/t/appbits-trackweight-turns-your-trackpad-into-a-scale-sort-of/31609)
- [MacRumors: Apple Drops Force Touch Gesture in watchOS 7](https://www.macrumors.com/2020/06/23/apple-drops-force-touch-gesture-in-watchos-7/)

## 八、架構決定（2026-11-06，Nocehi 同意）

比照 AppKit 分工：監聽器只負責廣播壓力階段（含類 `stageTransition` 進度）並提供觸覺服務；要做什麼動作，由 DMS、查詢卡片等 GUI 層依游標下的物件決定。第六節的 Dock、加速器兩項以此層為前提。
