#!/usr/bin/env python3
"""Fire one haptic pulse on an Apple Magic Trackpad (USB) via hidraw.

Experiment only. Finds the "Actuator" HID interface of 05ac:0324 (or 0265)
by looking for output report 0x53 in its report descriptor, then writes the
14-byte report used by AppleMagicTouchstreamLP (validated on Linux, USB only):

    53 01 <strength u32 LE> 21 2B 06 01 00 16 41 13

Default strength 0x00026C19 is that project's "force" payload.
Without --fire it only prints what it found. hidraw nodes are usually
root-only, so run with sudo for the first test.

Usage:  sudo python3 haptic_pulse.py --fire [--count 3] [--interval 0.25]
"""
import argparse, glob, os, struct, sys, time

APPLE = 0x05AC
PIDS = {0x0324, 0x0265}
TAIL = bytes([0x21, 0x2B, 0x06, 0x01, 0x00, 0x16, 0x41, 0x13])


def hid_id(sys_hidraw):
    with open(os.path.join(sys_hidraw, "device", "uevent")) as f:
        for line in f:
            if line.startswith("HID_ID="):
                _, v, p = line.strip().split("=")[1].split(":")
                return int(v, 16), int(p, 16)
    return None, None


def has_output_0x53(sys_hidraw):
    with open(os.path.join(sys_hidraw, "device", "report_descriptor"), "rb") as f:
        d = f.read()
    # Walk short items; track current Report ID, look for Output (0x91) under ID 0x53.
    i, rid = 0, None
    while i < len(d):
        b = d[i]
        size = {0: 0, 1: 1, 2: 2, 3: 4}[b & 3]
        tag = b & 0xFC
        val = int.from_bytes(d[i + 1:i + 1 + size], "little") if size else 0
        if tag == 0x84:
            rid = val
        elif tag == 0x90 and rid == 0x53:
            return True
        i += 1 + size
    return False


def find_actuator():
    for s in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        v, p = hid_id(s)
        if v == APPLE and p in PIDS and has_output_0x53(s):
            return "/dev/" + os.path.basename(s), p
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fire", action="store_true")
    ap.add_argument("--strength", type=lambda x: int(x, 0), default=0x00026C19)
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--interval", type=float, default=0.25)
    a = ap.parse_args()

    node, pid = find_actuator()
    if not node:
        sys.exit("No USB Magic Trackpad actuator interface (report 0x53) found. "
                 "Over Bluetooth Linux does not expose it.")
    report = bytes([0x53, 0x01]) + struct.pack("<I", a.strength) + TAIL
    print(f"actuator: {node} (05ac:{pid:04x})  report: {report.hex(' ')}")
    if not a.fire:
        print("dry run; add --fire to send")
        return
    fd = os.open(node, os.O_WRONLY)
    try:
        for n in range(max(1, a.count)):
            os.write(fd, report)
            if n + 1 < a.count:
                time.sleep(a.interval)
    finally:
        os.close(fd)
    print("sent")


if __name__ == "__main__":
    main()
