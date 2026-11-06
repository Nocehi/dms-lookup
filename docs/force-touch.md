# Force-press lookup (experimental)

> **Status: experimental dogfood, off by default.** Tested on one Apple Magic
> Trackpad (A3120, USB-C, `05ac:0324`) over **USB** on CachyOS / niri 26.04 /
> libinput 1.32. Haptic feedback works over USB and, since 2026-11-06, over
> Bluetooth (the watcher sends the same report behind ID `0xF2` when the touch
> device is on Bluetooth). `packaging/install-user.sh`
> does not install or enable anything described here, and
> `packaging/uninstall-user.sh` does not remove it.

Press a selection firmly on a Magic Trackpad (a "deep press", as in macOS Force
Click) and Dank Lookup opens on release, with a short haptic tick when the
press counts as deep. It runs the same `dank-lookup-selection` command as the
keyboard shortcut, with the source name `force-press`, which never reads the
clipboard (see [`privacy.md`](privacy.md)).

## How it decides

`bin/dank-force-press-watch` reads the trackpad's evdev node without grabbing
it, so the cursor and clicks behave normally. A press is deep when, while the
button is held:

- exactly one finger is on the pad;
- its pressure stays at or above `--force` (default 160) for `--hold` ms
  (default 80);
- that finger has not moved more than `--max-move` mm (default 3) since the
  button went down, so a hard drag-select is not a deep press.

The margin is on the time axis, not the pressure axis: in natural use, ordinary
clicks stayed at ≥ 160 for at most 44 ms while deep presses held for 265 ms or
more (34 presses, 5 deep; a thin sample). The lookup fires on release so a drag
selection is finished first; a 400 ms cooldown stops double fires.

The watcher logs pressure, timing and trackpad travel only, never text.

## Install

Requirements: `packaging/install-user.sh` already run, `python-evdev`, and a
trackpad event node (`/dev/input/eventN`) readable by your user, as it is for
the logged-in seat user on the tested setup.

```sh
packaging/install-force-touch.sh            # copy watcher + unit, leave disabled
~/.local/bin/dank-force-press-watch --dry-run -v   # watch the log, press a few times
systemctl --user enable --now dank-force-press.service
```

`--enable` does the last step for you. `--dry-run` on the installer prints what
it would do. An existing file that differs is kept as `<name>.bak-<timestamp>`.

Haptics (optional) need write access to the trackpad's `hidraw` node: the
actuator interface over USB, the single node over Bluetooth. The rule covers
both (`KERNELS=="0005:004C:0324.*"` for Bluetooth, which has no USB
attributes to match). The installer prints the commands; it never runs sudo:

```sh
sudo install -m 644 packaging/udev/70-dank-magic-trackpad-haptic.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=hidraw
```

Check: `journalctl --user -u dank-force-press -n 5` should show
`haptic on /dev/hidrawN`. `not writable` means the rule has not applied yet;
replug the cable, or reconnect over Bluetooth. `--strength` applies to both.

## Tune

Edit `ExecStart` with `systemctl --user edit --full dank-force-press`, then
restart the service.

| Symptom | Option |
|---|---|
| ordinary clicks fire lookups | `--force 175` or `--hold 100` |
| deep presses are hard to get | lower `--force` |
| drag-select still fires | lower `--max-move` |
| pulse too strong / weak | `--strength 0x...` (default `0x26C19`) |
| no pulse wanted | `--no-haptic` |

## Uninstall

```sh
packaging/install-force-touch.sh --uninstall
sudo rm /etc/udev/rules.d/70-dank-magic-trackpad-haptic.rules && sudo udevadm control --reload
```

## Known limits

- One device model. Bluetooth haptics verified with `haptic_pulse.py` only;
  the watcher's Bluetooth path is not yet dogfooded, and pressure thresholds
  and report rate over Bluetooth have not been re-measured.
- A resting thumb on the pad counts as a second finger and blocks the deep
  press.
- Something must already be selected; a click does not select a word.
- Primary-selection retention after the first click in Firefox and Qt apps is
  untested.

## Tools and design notes

- `tools/force-touch/`: read-only probes used for the measurements
  (`pressure_probe.py`, `margin_probe.py`, `area_probe.py`) and
  `haptic_pulse.py` (lists actuator nodes; `--fire` sends one pulse).
- [`force-touch/notes.md`](force-touch/notes.md): tests, measurements, existing
  solutions, kernel history, roadmap (Chinese).
- [`force-touch/force-event-bus-design.md`](force-touch/force-event-bus-design.md):
  planned force event bus with Dank Lookup and DMS as consumers (Chinese).
- [`force-touch/apple-force-touch-survey.md`](force-touch/apple-force-touch-survey.md):
  how Apple uses Force Touch and what carries over to niri/DMS (Chinese).
