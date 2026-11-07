#!/usr/bin/env python3
"""Passive margin probe for Magic Trackpad force press (natural use).

Non-grabbing, read-only: cursor, clicks and the dank-force-press service
keep working. Run it in a terminal, then just use the computer normally
(browse, select text, click buttons) for 10+ minutes; Ctrl+C to stop.

Per click (BTN_LEFT down -> up) it records:
  peak            highest pressure of the primary touch (= highest-pressure
                  touch; a resting thumb < --rest never wins)
  run_ms          longest continuous time primary pressure >= --force
  svc_run_ms      same, but only while exactly one touch is down (the
                  installed service's rule); svc_deep = svc_run_ms >= --hold
  touches         max touches down during the click; active = touches whose
                  own peak >= --rest (resting thumb excluded)
Plus every frame of every click (+ 100 ms before) in frames.csv, so any
threshold can be re-computed afterwards. Logs pressure numbers and timing
only; never reads text, never logs positions.

Usage: python3 margin_probe.py [--force 160] [--hold 80] [--rest 50]
                               [--out /tmp/dank-force-touch/margin]
"""
import argparse, collections, csv, os, statistics, sys, time
import evdev
from evdev import ecodes as E

APPLE_VENDORS = {0x004C, 0x05AC}
TRACKPADS = {0x0265, 0x0324}


def find_device():
    for path in evdev.list_devices():
        try:
            d = evdev.InputDevice(path)
        except OSError:
            continue  # MT2 USB interface 0 returns EPIPE on open
        if d.info.vendor in APPLE_VENDORS and d.info.product in TRACKPADS:
            if any(c == E.ABS_MT_PRESSURE
                   for c, _ in d.capabilities().get(E.EV_ABS, [])):
                return d
        d.close()
    sys.exit("no Magic Trackpad node with ABS_MT_PRESSURE found")


class Run:
    """Longest continuous span where cond holds, on frame timestamps."""

    def __init__(self):
        self.start = None
        self.best = 0.0

    def step(self, t, cond):
        if cond and self.start is None:
            self.start = t
        elif not cond and self.start is not None:
            self.best = max(self.best, t - self.start)
            self.start = None

    def close(self, t):
        self.step(t, False)
        return int(round(self.best * 1000))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", type=int, default=160)
    ap.add_argument("--hold", type=int, default=80)
    ap.add_argument("--rest", type=int, default=50,
                    help="touches whose peak stays below this are a resting thumb")
    ap.add_argument("--out", default="/tmp/dank-force-touch/margin")
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    pf = open(os.path.join(a.out, f"presses-{stamp}.csv"), "w", newline="")
    ff = open(os.path.join(a.out, f"frames-{stamp}.csv"), "w", newline="")
    pw, fw = csv.writer(pf), csv.writer(ff)
    pw.writerow(["id", "wall", "dur_ms", "peak", "run_ms", "svc_run_ms",
                 "svc_deep", "first_cross_ms", "touches", "active", "force", "hold"])
    fw.writerow(["id", "t_ms", "p_primary", "n_touches", "btn"])

    dev = find_device()
    print(f"{dev.path} {dev.name!r}; force>={a.force} hold={a.hold}ms rest<{a.rest}")
    print(f"writing {pf.name}\nuse the trackpad normally; Ctrl+C to stop\n", flush=True)

    slot, slots, pressure, tpeak = 0, set(), {}, {}
    btn = False
    pre = collections.deque()           # (t, p, n) frames before the click
    cur = None
    n_press = n_deep = 0
    peaks, runs = [], []

    try:
        for ev in dev.read_loop():
            if ev.type == E.EV_ABS:
                if ev.code == E.ABS_MT_SLOT:
                    slot = ev.value
                elif ev.code == E.ABS_MT_TRACKING_ID:
                    if ev.value == -1:
                        slots.discard(slot); pressure.pop(slot, None)
                    else:
                        slots.add(slot); pressure[slot] = 0
                elif ev.code == E.ABS_MT_PRESSURE:
                    pressure[slot] = ev.value
            elif ev.type == E.EV_KEY and ev.code in (E.BTN_LEFT, E.BTN_MOUSE):
                btn = bool(ev.value)
            elif ev.type == E.EV_SYN and ev.code == E.SYN_REPORT:
                t = ev.timestamp()
                p = max(pressure.values(), default=0)
                n = len(slots)
                if btn and cur is None:
                    n_press += 1
                    cur = {"id": n_press, "t0": t, "wall": time.strftime("%H:%M:%S"),
                           "peak": 0, "run": Run(), "svc": Run(), "cross": None,
                           "touches": 0, "tpeak": collections.defaultdict(int)}
                    for (tt, pp, nn) in pre:
                        fw.writerow([n_press, round((tt - t) * 1000, 1), pp, nn, 0])
                    pre.clear()
                if cur:
                    above = p >= a.force
                    cur["peak"] = max(cur["peak"], p)
                    cur["run"].step(t, above)
                    cur["svc"].step(t, above and n == 1)
                    if above and cur["cross"] is None:
                        cur["cross"] = int((t - cur["t0"]) * 1000)
                    cur["touches"] = max(cur["touches"], n)
                    for s in slots:
                        cur["tpeak"][s] = max(cur["tpeak"][s], pressure.get(s, 0))
                    fw.writerow([cur["id"], round((t - cur["t0"]) * 1000, 1), p, n, int(btn)])
                    if not btn:
                        run_ms, svc_ms = cur["run"].close(t), cur["svc"].close(t)
                        deep = svc_ms >= a.hold
                        active = sum(1 for v in cur["tpeak"].values() if v >= a.rest)
                        pw.writerow([cur["id"], cur["wall"], int((t - cur["t0"]) * 1000),
                                     cur["peak"], run_ms, svc_ms, int(deep),
                                     "" if cur["cross"] is None else cur["cross"],
                                     cur["touches"], active, a.force, a.hold])
                        pf.flush(); ff.flush()
                        n_deep += deep
                        peaks.append(cur["peak"]); runs.append(run_ms)
                        print(f"\r{n_press} clicks, {n_deep} deep", end="", flush=True)
                        cur = None
                else:
                    pre.append((t, p, n))
                    while pre and t - pre[0][0] > 0.1:
                        pre.popleft()
    except (KeyboardInterrupt, OSError) as e:
        if isinstance(e, OSError):
            print(f"\ndevice lost ({e})")
    finally:
        pf.close(); ff.close()

    print(f"\n\n{n_press} clicks, {n_deep} classified deep (svc rule)")
    if peaks:
        print(f"peak:   median {statistics.median(peaks)}  max {max(peaks)}")
        print(f"run_ms: median {statistics.median(runs)}  max {max(runs)}")
    print(f"files: {pf.name}\n       {ff.name}")


if __name__ == "__main__":
    main()
