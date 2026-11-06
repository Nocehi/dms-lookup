#!/usr/bin/env python3
"""Pressure vs contact-area probe for Apple Magic Trackpad 2 (0x0265 / 0x0324).

Read-only, does not grab the device. Guides you through three phases of
10 single-finger presses each and writes every frame to a CSV:

  phase rest   light touch, no click
  phase click  normal click
  phase deep   click, then press as hard as for a force click

Usage:  python3 area_probe.py [out.csv]      (default /tmp/dank-force-touch/area.csv)
Ctrl+C ends early; what was recorded is kept.
"""
import os, sys, time, csv
import evdev
from evdev import ecodes as E

APPLE_VENDORS = {0x004C, 0x05AC}
TRACKPADS = {0x0265, 0x0324}
PHASES = [("rest", "輕放手指，不按下"), ("click", "普通點擊"), ("deep", "點擊後用力深壓")]
PER_PHASE = 10
FIELDS = {E.ABS_MT_PRESSURE: "pressure", E.ABS_MT_TOUCH_MAJOR: "major",
          E.ABS_MT_TOUCH_MINOR: "minor", E.ABS_MT_WIDTH_MAJOR: "wmajor",
          E.ABS_MT_WIDTH_MINOR: "wminor"}


def find_device():
    for path in evdev.list_devices():
        try:
            d = evdev.InputDevice(path)
        except OSError:
            continue
        if d.info.vendor in APPLE_VENDORS and d.info.product in TRACKPADS:
            caps = d.capabilities().get(E.EV_ABS, [])
            if any(c == E.ABS_MT_PRESSURE for c, _ in caps):
                return d
    sys.exit("no Magic Trackpad 2 node with ABS_MT_PRESSURE found")


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/dank-force-touch/area.csv"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    dev = find_device()
    absinfo = dict(dev.capabilities(absinfo=True)[E.EV_ABS])
    print(f"device: {dev.path} {dev.name!r}")
    for code, name in FIELDS.items():
        a = absinfo.get(code)
        print(f"  {name:8} {'missing' if a is None else f'{a.min}..{a.max}'}")

    f = open(out, "w", newline="")
    w = csv.writer(f)
    w.writerow(["phase", "ep", "t_ms", "fingers", "btn", *FIELDS.values()])

    slot, slots, vals = 0, set(), {}
    btn = False
    phase_i, ep_in_phase, ep_frames, multi = 0, 0, [], False
    t0 = None

    def announce():
        name, desc = PHASES[phase_i]
        print(f"\n== 第 {phase_i+1}/3 組 [{name}]：{desc}，共 {PER_PHASE} 次，每次之間停約 1 秒 ==")
    announce()
    try:
        for ev in dev.read_loop():
            if ev.type == E.EV_ABS:
                if ev.code == E.ABS_MT_SLOT:
                    slot = ev.value
                elif ev.code == E.ABS_MT_TRACKING_ID:
                    if ev.value == -1:
                        slots.discard(slot); vals.pop(slot, None)
                    else:
                        slots.add(slot); vals[slot] = {}
                elif ev.code in FIELDS:
                    vals.setdefault(slot, {})[FIELDS[ev.code]] = ev.value
            elif ev.type == E.EV_KEY and ev.code in (E.BTN_LEFT, E.BTN_MOUSE):
                btn = bool(ev.value)
            elif ev.type == E.EV_SYN and ev.code == E.SYN_REPORT:
                n = len(slots)
                if n and t0 is None:
                    t0, ep_frames, multi = time.monotonic(), [], False
                if t0 is None:
                    continue
                multi |= n > 1
                if n == 1:
                    v = vals[next(iter(slots))]
                    ep_frames.append([int((time.monotonic() - t0) * 1000), n, int(btn),
                                      *(v.get(k, "") for k in FIELDS.values())])
                if n == 0 and not btn:
                    t0 = None
                    if multi or not ep_frames:
                        print("  （多指或空白，忽略）")
                        continue
                    ep_in_phase += 1
                    name = PHASES[phase_i][0]
                    for fr in ep_frames:
                        w.writerow([name, ep_in_phase, *fr])
                    f.flush()
                    pk = max(ep_frames, key=lambda r: r[3] or 0)
                    clicked = any(r[2] for r in ep_frames)
                    print(f"  {ep_in_phase:>2}  peak p={pk[3]} major={pk[4]} minor={pk[5]}"
                          f"  clicked={clicked}", flush=True)
                    if ep_in_phase >= PER_PHASE:
                        phase_i, ep_in_phase = phase_i + 1, 0
                        if phase_i >= len(PHASES):
                            break
                        announce()
    except KeyboardInterrupt:
        pass
    f.close()
    print(f"\n已寫入 {out}")


if __name__ == "__main__":
    main()
