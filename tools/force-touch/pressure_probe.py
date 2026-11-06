#!/usr/bin/env python3
"""Read-only pressure probe for Apple Magic Trackpad 2 (0x0265 / 0x0324).

Does not grab the device: the cursor and clicks keep working normally.
Prints one line per finger-contact episode, then a summary.

Usage:  python3 pressure_probe.py [seconds]   (default 90; needs `input` group;
        Ctrl+C ends early and still prints the summary)
Suggested sequence while it runs (~10 each, pause ~1 s between):
  1. light touch, no click
  2. normal click
  3. click, then press as hard as for a macOS force click
"""
import sys, time, statistics
import evdev
from evdev import ecodes as E

APPLE_VENDORS = {0x004C, 0x05AC}
TRACKPADS = {0x0265, 0x0324}


def find_device():
    for path in evdev.list_devices():
        try:
            d = evdev.InputDevice(path)
        except OSError:
            continue  # e.g. MT2 USB interface 0 returns EPIPE on open
        if d.info.vendor in APPLE_VENDORS and d.info.product in TRACKPADS:
            caps = d.capabilities().get(E.EV_ABS, [])
            if any(c == E.ABS_MT_PRESSURE for c, _ in caps):
                return d
    sys.exit("no Magic Trackpad 2 node with ABS_MT_PRESSURE found")


def main():
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 90.0
    dev = find_device()
    info = dict(dev.capabilities(absinfo=True)[E.EV_ABS])
    p = info[E.ABS_MT_PRESSURE]
    print(f"device: {dev.path} {dev.name!r} vendor={dev.info.vendor:#06x} "
          f"product={dev.info.product:#06x} bustype={dev.info.bustype:#x}")
    print(f"ABS_MT_PRESSURE range {p.min}..{p.max}")
    print(f"recording {duration:.0f}s ...\n")
    print("ep  fingers  clicked  max_before_click  p_at_click  max_while_clicked  dur_ms")

    slot, slots = 0, {}
    pressure = {}
    btn = False
    ep = None  # current episode
    episodes = []
    end = time.monotonic() + duration

    def start_ep():
        return {"t0": time.monotonic(), "fingers": 0, "clicked": False,
                "pre": 0, "at": None, "during": 0}

    try:
        for ev in dev.read_loop():
            if time.monotonic() > end:
                break
            if ev.type == E.EV_ABS:
                if ev.code == E.ABS_MT_SLOT:
                    slot = ev.value
                elif ev.code == E.ABS_MT_TRACKING_ID:
                    if ev.value == -1:
                        slots.pop(slot, None); pressure.pop(slot, None)
                    else:
                        slots[slot] = ev.value
                elif ev.code == E.ABS_MT_PRESSURE:
                    pressure[slot] = ev.value
            elif ev.type == E.EV_KEY and ev.code in (E.BTN_LEFT, E.BTN_MOUSE):
                btn = bool(ev.value)
            elif ev.type == E.EV_SYN and ev.code == E.SYN_REPORT:
                n = len(slots)
                if n and ep is None:
                    ep = start_ep()
                if ep is not None:
                    pmax = max(pressure.values(), default=0)
                    ep["fingers"] = max(ep["fingers"], n)
                    if btn:
                        if not ep["clicked"]:
                            ep["clicked"], ep["at"] = True, pmax
                        ep["during"] = max(ep["during"], pmax)
                    elif not ep["clicked"]:
                        ep["pre"] = max(ep["pre"], pmax)
                    if n == 0 and not btn:
                        ep["dur"] = int((time.monotonic() - ep["t0"]) * 1000)
                        episodes.append(ep)
                        print(f"{len(episodes):>2}  {ep['fingers']:>7}  {str(ep['clicked']):>7}  "
                              f"{ep['pre']:>16}  {str(ep['at']):>10}  {ep['during']:>17}  {ep['dur']:>6}",
                              flush=True)
                        ep = None
    except KeyboardInterrupt:
        pass

    def stats(name, xs):
        if xs:
            print(f"{name}: n={len(xs)} min={min(xs)} median={statistics.median(xs)} max={max(xs)}")
    one = [e for e in episodes if e["fingers"] == 1]
    print("\n== summary (single-finger episodes) ==")
    stats("no-click max pressure", [e["pre"] for e in one if not e["clicked"]])
    stats("pressure at click onset", [e["at"] for e in one if e["clicked"]])
    stats("max pressure while clicked", [e["during"] for e in one if e["clicked"]])


if __name__ == "__main__":
    main()
