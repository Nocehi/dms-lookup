#!/usr/bin/env python3
"""Fire a haptic pulse on an Apple Magic Trackpad over USB *or* Bluetooth.

Experiment only. Without --fire it only lists what it found and changes nothing.

Matching is by product ID (0x0324 USB-C, 0x0265 Lightning) under either Apple
vendor ID: 0x05AC on USB (bus 0003), 0x004C on Bluetooth (bus 0005). Device
names and hidraw numbers differ by transport, so neither is used.

Two report formats are known:

  usb  14 bytes, output report 0x53 on the separate "Actuator" interface
       (AppleMagicTouchstreamLP; verified on our A3120 over USB):
           53 01 <strength u32 LE> 21 2B 06 01 00 16 41 13
  bt   15 bytes: the usb report prefixed with report ID 0xF2 (verified on
       the A3120 over Bluetooth 2026-11-06; same feel as USB)
  mtp  15 bytes, Magic-TP-extended / LinuxMagicForce waveform (verified by
       them on 0265, and on our A3120 over Bluetooth; louder at 0x30):
           F2 53 01 <strength> 78 02 <sharp> 24 30 06 01 <deep> 18 48 12

--format auto picks bt on a Bluetooth link (the descriptor there does not
declare 0xF2, so it cannot be detected), usb if the node declares output report
0x53, and refuses otherwise (use --format to
force one; --dump shows the descriptor's report IDs so we learn either way).

Usage:
  python3 haptic_pulse.py                 # list nodes, dry run
  python3 haptic_pulse.py --dump          # also print report IDs and sizes
  python3 haptic_pulse.py --fire [--count 3] [--format auto|usb|bt] [--node /dev/hidrawN]
"""
import argparse, glob, os, struct, sys, time

VENDORS = {0x05AC: "usb-vendor", 0x004C: "bt-vendor"}
PIDS = {0x0324: "Magic Trackpad (USB-C, A3120)", 0x0265: "Magic Trackpad 2"}
BUSES = {0x0003: "USB", 0x0005: "Bluetooth"}
USB_TAIL = bytes([0x21, 0x2B, 0x06, 0x01, 0x00, 0x16, 0x41, 0x13])


def hid_id(sys_hidraw):
    """HID_ID=0005:0000004C:00000324 -> (bus, vendor, product)."""
    try:
        with open(os.path.join(sys_hidraw, "device", "uevent")) as f:
            for line in f:
                if line.startswith("HID_ID="):
                    b, v, p = line.strip().split("=", 1)[1].split(":")
                    return int(b, 16), int(v, 16), int(p, 16)
    except (OSError, ValueError):
        pass
    return None


def hid_name(sys_hidraw):
    try:
        with open(os.path.join(sys_hidraw, "device", "uevent")) as f:
            for line in f:
                if line.startswith("HID_NAME="):
                    return line.strip().split("=", 1)[1]
    except OSError:
        pass
    return "?"


def reports(sys_hidraw):
    """Walk the report descriptor; return {(kind, report_id): bits}.

    kind is 'in', 'out' or 'feat'. Handles short items, Push/Pop, skips long
    items. Bits are summed per report ID (without the ID byte)."""
    with open(os.path.join(sys_hidraw, "device", "report_descriptor"), "rb") as f:
        d = f.read()
    out, stack = {}, []
    g = {"rid": 0, "size": 0, "count": 0}
    i = 0
    while i < len(d):
        b = d[i]
        if b == 0xFE:  # long item
            i += 3 + (d[i + 1] if i + 1 < len(d) else 0)
            continue
        size = (0, 1, 2, 4)[b & 3]
        tag = b & 0xFC
        val = int.from_bytes(d[i + 1:i + 1 + size], "little") if size else 0
        if tag == 0x84:
            g["rid"] = val
        elif tag == 0x74:
            g["size"] = val
        elif tag == 0x94:
            g["count"] = val
        elif tag == 0xA4:
            stack.append(dict(g))
        elif tag == 0xB4 and stack:
            g = stack.pop()
        elif tag in (0x80, 0x90, 0xB0):
            kind = {0x80: "in", 0x90: "out", 0xB0: "feat"}[tag]
            key = (kind, g["rid"])
            out[key] = out.get(key, 0) + g["size"] * g["count"]
        i += 1 + size
    return out


def candidates():
    found = []
    for s in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        ids = hid_id(s)
        if not ids:
            continue
        bus, v, p = ids
        if v in VENDORS and p in PIDS:
            try:
                reps = reports(s)
            except OSError:
                reps = {}
            found.append({"sys": s, "node": "/dev/" + os.path.basename(s),
                          "bus": bus, "vid": v, "pid": p, "name": hid_name(s),
                          "reps": reps})
    return found


def fmt_for(c):
    # Over Bluetooth the descriptor does not declare 0xF2 (seen on the A3120:
    # only in 0x02, feat 0x55, in 0x90); external tools write it regardless,
    # and hidraw does not check. So a Bluetooth link defaults to bt.
    if c.get("bus") == 0x0005:
        return "bt"
    if ("out", 0x53) in c["reps"]:
        return "usb"
    return None


def build(fmt, a):
    usb = bytes([0x53, 0x01]) + struct.pack("<I", a.strength) + USB_TAIL
    if fmt == "usb":
        return usb
    if fmt == "bt":
        return b"\xF2" + usb
    return bytes([0xF2, 0x53, 0x01, a.bt_strength, 0x78, 0x02, a.sharp, 0x24,
                  0x30, 0x06, 0x01, a.deep, 0x18, 0x48, 0x12])


def main():
    byte = lambda x: int(x, 0) & 0xFF
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fire", action="store_true", help="actually send")
    ap.add_argument("--dump", action="store_true", help="print report IDs/sizes")
    ap.add_argument("--node", help="use this /dev/hidrawN instead of auto")
    ap.add_argument("--format", choices=["auto", "usb", "bt", "mtp"], default="auto")
    ap.add_argument("--strength", type=lambda x: int(x, 0), default=0x00026C19,
                    help="usb/bt format strength u32 (default 0x00026C19)")
    ap.add_argument("--bt-strength", type=byte, default=0x30,
                    help="mtp format byte 3 (Magic-TP-extended default 0x30)")
    ap.add_argument("--sharp", type=byte, default=0x10, help="mtp byte 6, keep <= 0x20")
    ap.add_argument("--deep", type=byte, default=0x10, help="mtp byte 11, keep <= 0x40")
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--interval", type=float, default=0.25)
    a = ap.parse_args()

    cands = candidates()
    if not cands:
        sys.exit("No Magic Trackpad hidraw node (vendor 05ac/004c, product 0324/0265) found.\n"
                 "Over Bluetooth: is it paired AND connected? Check `bluetoothctl info`.")
    chosen = None
    for c in cands:
        f = fmt_for(c)
        w = os.access(c["node"], os.W_OK)
        print(f"{c['node']}  {BUSES.get(c['bus'], hex(c['bus']))}  "
              f"{c['bus']:04x}:{c['vid']:04x}:{c['pid']:04x}  {c['name']!r}  "
              f"format={f or '-'}  writable={'yes' if w else 'no'}")
        if a.dump:
            for (kind, rid), bits in sorted(c["reps"].items(), key=lambda k: (k[0][1], k[0][0])):
                print(f"    {kind:4} id 0x{rid:02X}  {bits // 8} bytes")
        if a.node and c["node"] == a.node:
            chosen = c
        elif not a.node and chosen is None and f:
            chosen = c
    if a.node and chosen is None:
        sys.exit(f"{a.node} is not one of the nodes above")
    if chosen is None:
        sys.exit("No USB node declares output 0x53 and no Bluetooth node found. "
                 "Run with --dump; you can still force one with --node /dev/hidraw1 --format bt.")
    fmt = chosen and (a.format if a.format != "auto" else fmt_for(chosen))
    if not fmt:
        sys.exit(f"{chosen['node']}: format unknown; pass --format usb or --format bt")
    report = build(fmt, a)
    print(f"\nchosen: {chosen['node']} ({BUSES.get(chosen['bus'], '?')})  "
          f"format={fmt}  report: {report.hex(' ')}")
    if not a.fire:
        print("dry run; add --fire to send")
        return
    try:
        fd = os.open(chosen["node"], os.O_WRONLY)
    except PermissionError:
        sys.exit(f"{chosen['node']} not writable: udev rule does not cover it; "
                 "retry with sudo to test the haptic itself")
    try:
        for n in range(max(1, a.count)):
            os.write(fd, report)
            if n + 1 < a.count:
                time.sleep(a.interval)
    except OSError as e:
        sys.exit(f"write failed: {e}  (errno {e.errno})")
    finally:
        os.close(fd)
    print(f"sent {a.count}; did the trackpad click?")


if __name__ == "__main__":
    main()
