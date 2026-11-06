# A3120 藍牙觸覺測試步驟

目的：確定 A3120（產品 ID `0324`）經藍牙連線時能否按需觸發觸覺。外部專案只在 `0265` 經藍牙時驗證過（見 [`notes.md`](notes.md) 2.1）。

相關檔案：

| 檔案 | 用途 |
|---|---|
| `tools/force-touch/haptic_pulse.py` | 觸覺實驗腳本。按產品 ID 配對，USB（`05ac`）與藍牙（`004c`）都認；預設只列出節點，不發送 |
| `packaging/udev/70-dank-magic-trackpad-haptic.rules` | 現行 udev 規則，只配對 USB |

以下指令假設腳本放在 `/tmp/dank-force-touch/bt-haptics/`。第四節的藍牙規則尚未驗證，所以未放進 `packaging/`，測試時另存為 `70-dank-magic-trackpad-haptic.rules`：

```
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="05ac", ATTRS{idProduct}=="0324", TAG+="uaccess"
SUBSYSTEM=="hidraw", KERNELS=="0005:004C:0324.*", TAG+="uaccess"
```

## 一、USB 基準（觸控板仍插線）

```sh
cd /tmp/dank-force-touch/bt-haptics
python3 haptic_pulse.py --dump
```

預期看到 `/dev/hidraw3 USB 0003:05ac:0324 ... format=usb writable=yes`，以及 `out id 0x53 14 bytes`。把整段輸出記下，作對照。

## 二、改用藍牙

1. `bluetoothctl`，依序 `power on`、`scan on`。把觸控板關機再開機，等它出現（名稱多半是 "Magic Trackpad"），記下 MAC。
2. `pair <MAC>`、`trust <MAC>`、`connect <MAC>`，然後 `quit`。
3. 拔掉 USB 線。確認游標仍能移動。

查看節點：

```sh
grep -l 'HID_ID=0005' /sys/class/hidraw/hidraw*/device/uevent
python3 haptic_pulse.py --dump
```

預期出現一行 `Bluetooth 0005:004c:0324`。注意 hidraw 編號會和 USB 時不同。

## 三、試震

先用一般用戶執行（同時測試 udev 規則）：

```sh
python3 haptic_pulse.py --fire --count 3
```

- 若顯示 `not writable`：現行規則沒有涵蓋藍牙節點（預期如此，見第四節）。改用 `sudo python3 haptic_pulse.py --fire --count 3` 測觸覺本身。
- 若顯示 `No node declares output 0x53 or report 0xF2`：把 `--dump` 輸出貼給我。仍可手動試：
  `sudo python3 haptic_pulse.py --node /dev/hidrawN --format bt --fire`，再試 `--format usb`。
- 若顯示 `write failed`：記下錯誤碼。

請記下：`--dump` 的完整輸出、選了哪個格式、有沒有震、震感和 USB 時比較如何。

可選：調強弱比較手感，例如 `--bt-strength 0x20`、`--bt-strength 0x40`。

## 四、udev 規則是否涵蓋藍牙節點

```sh
udevadm info -a -n /dev/hidrawN | grep -E 'KERNELS=="0005|idVendor'
getfacl /dev/hidrawN
```

現行規則只用 `ATTRS{idVendor}=="05ac"` 配對。藍牙節點上沒有 USB 祖先，這個屬性不存在，所以預期**不會**套用（`getfacl` 看不到 `user:nocehi:rw-`）。linux-trackpad-driver 的安裝說明也指出了這一點。

若藍牙能震，想讓一般用戶也能震，可換上上面的兩行規則（會改動 `/etc`，由你決定）：

```sh
sudo install -m 644 70-dank-magic-trackpad-haptic.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=hidraw
getfacl /dev/hidrawN    # 應出現 user:nocehi:rw-
python3 haptic_pulse.py --fire
```

注意：現時安裝的監聽程式只在 `05ac` 底下找觸覺節點，所以即使藍牙能震，深壓查詢在藍牙下仍然不會震。查詢本身照常運作（它的 evdev 配對已涵蓋 `004c`）。要改監聽程式，等這次結果出來再說。

## 五、可選：量藍牙回報率

`margin_probe.py` 的 evdev 配對已涵蓋藍牙。照平常用法用 10 分鐘以上，不刻意按：

```sh
python3 /tmp/dank-force-touch/margin_probe.py --out /tmp/dank-force-touch/margin-bt
```

（repo 內為 `tools/force-touch/margin_probe.py`。）

結束後把 `margin-bt/` 的兩個 CSV 交給我。我會從 `frames-*.csv` 的相鄰 `t_ms` 差算回報間隔。USB 基準已有：既有 USB 數據的中位間隔 11.0 ms（約 91 Hz，第 10–90 百分位 10.7–12.0 ms，n=1169）。注意這只是「按住時有變化的幀」的間隔，不是韌體標稱回報率。也可順便看藍牙下的峰值與 ≥160 持續時間分佈是否偏移，門檻是否要重新量。

## 六、復原

- 重新插上 USB 線即可回到原狀；藍牙配對可保留，或 `bluetoothctl remove <MAC>`。
- 若換過 udev 規則，想還原：裝回只有 USB 一行的舊規則，再 reload/trigger。
