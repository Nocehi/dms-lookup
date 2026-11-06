#!/usr/bin/env python3
"""Experimental force-press trigger: opt-in boundary and deep-press rule.

The watcher is driven with synthetic evdev events through a stub `evdev`
module, so no trackpad or python-evdev is needed.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parent.parent
WATCHER = ROOT / "bin/dank-force-press-watch"
UNIT = ROOT / "systemd/dank-force-press.service"
UDEV = ROOT / "packaging/udev/70-dank-magic-trackpad-haptic.rules"
INSTALLER = ROOT / "packaging/install-force-touch.sh"

CODES = dict(EV_SYN=0, EV_KEY=1, EV_ABS=3, SYN_REPORT=0, BTN_LEFT=0x110,
             BTN_MOUSE=0x110, ABS_MT_SLOT=0x2F, ABS_MT_POSITION_X=0x35,
             ABS_MT_POSITION_Y=0x36, ABS_MT_TRACKING_ID=0x39, ABS_MT_PRESSURE=0x3A)
E = types.SimpleNamespace(**CODES)
RES = 47  # units per mm, matches the A3120


def load_watcher():
    evdev = types.ModuleType("evdev")
    evdev.ecodes = E
    with mock.patch.dict(sys.modules, {"evdev": evdev}):
        loader = importlib.machinery.SourceFileLoader("dank_force_press_watch", str(WATCHER))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
    return module


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeDevice:
    """Replays (ms, type, code, value) tuples, advancing the clock."""

    def __init__(self, events, clock):
        self.events, self.clock = events, clock

    def absinfo(self, _code):
        return types.SimpleNamespace(resolution=RES)

    def read_loop(self):
        for ms, etype, code, value in self.events:
            self.clock.now = 1000.0 + ms / 1000
            yield types.SimpleNamespace(type=etype, code=code, value=value)


def press(pressures, *, step_ms=10, move_mm=0.0, t0=0, fingers=1):
    """One click: button down, a frame per pressure value, button up.

    The finger slides linearly by `move_mm` over the press.
    """
    ev = []
    for slot in range(fingers):
        ev += [(t0, E.EV_ABS, E.ABS_MT_SLOT, slot),
               (t0, E.EV_ABS, E.ABS_MT_TRACKING_ID, 10 + slot),
               (t0, E.EV_ABS, E.ABS_MT_POSITION_X, 1000 + 500 * slot),
               (t0, E.EV_ABS, E.ABS_MT_POSITION_Y, 1000),
               (t0, E.EV_ABS, E.ABS_MT_PRESSURE, 40)]
    ev += [(t0, E.EV_SYN, E.SYN_REPORT, 0), (t0, E.EV_KEY, E.BTN_LEFT, 1)]
    n = len(pressures)
    t = t0
    for i, p in enumerate(pressures):
        t = t0 + (i + 1) * step_ms
        x = 1000 + int(move_mm * RES * (i + 1) / n)
        ev += [(t, E.EV_ABS, E.ABS_MT_SLOT, 0),
               (t, E.EV_ABS, E.ABS_MT_PRESSURE, p),
               (t, E.EV_ABS, E.ABS_MT_POSITION_X, x),
               (t, E.EV_SYN, E.SYN_REPORT, 0)]
    t += step_ms
    ev.append((t, E.EV_KEY, E.BTN_LEFT, 0))
    for slot in range(fingers):
        ev += [(t, E.EV_ABS, E.ABS_MT_SLOT, slot),
               (t, E.EV_ABS, E.ABS_MT_TRACKING_ID, -1)]
    ev.append((t, E.EV_SYN, E.SYN_REPORT, 0))
    return ev


class WatcherRuleTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_watcher()
        self.args = types.SimpleNamespace(force=160, hold=80, max_move=3.0, cooldown=400,
                                          dry_run=False, verbose=False)
        self.haptic = mock.Mock()

    def fire_count(self, events):
        clock = Clock()
        with mock.patch.object(self.mod.time, "monotonic", clock), \
                mock.patch.object(self.mod.subprocess, "Popen") as popen, \
                mock.patch.object(self.mod, "log"):
            self.mod.run(FakeDevice(events, clock), self.args, "/x/dank-lookup-selection",
                         self.haptic)
        for call in popen.call_args_list:
            self.assertEqual(call.args[0], ["/x/dank-lookup-selection", "force-press"])
        return popen.call_count

    def test_deep_press_fires_once_with_force_press_source(self):
        self.assertEqual(self.fire_count(press([120] + [200] * 12)), 1)
        self.haptic.pulse.assert_called()

    def test_short_spike_above_threshold_does_not_fire(self):
        # ordinary clicks stay >= 160 for at most ~44 ms in natural use
        self.assertEqual(self.fire_count(press([120, 175, 180, 178, 170, 120, 90])), 0)
        self.haptic.pulse.assert_not_called()

    def test_light_press_does_not_fire(self):
        self.assertEqual(self.fire_count(press([100] * 30)), 0)

    def test_drag_select_does_not_fire(self):
        self.assertEqual(self.fire_count(press([200] * 20, move_mm=10.0)), 0)

    def test_small_travel_within_guard_still_fires(self):
        self.assertEqual(self.fire_count(press([200] * 20, move_mm=1.0)), 1)

    def test_two_fingers_do_not_fire(self):
        self.assertEqual(self.fire_count(press([200] * 20, fingers=2)), 0)

    def test_cooldown_blocks_second_fire(self):
        events = press([200] * 12) + press([200] * 12, t0=200) + press([200] * 12, t0=1000)
        self.assertEqual(self.fire_count(events), 2)

    def test_dry_run_never_spawns(self):
        self.args.dry_run = True
        self.assertEqual(self.fire_count(press([200] * 12)), 0)


class HapticDiscoveryTest(unittest.TestCase):
    """Actuator lookup over a fake /sys/class/hidraw (USB and Bluetooth)."""

    def setUp(self):
        self.w = load_watcher()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)

    def node(self, name, hid_id, descriptor=b""):
        d = self.root / name / "device"
        d.mkdir(parents=True)
        (d / "uevent").write_text(f"HID_ID={hid_id}\n")
        (d / "report_descriptor").write_bytes(descriptor)

    def haptic(self, bus):
        real_glob = self.w.glob.glob
        opened = []
        with mock.patch.object(self.w.glob, "glob",
                               lambda _p: real_glob(str(self.root / "hidraw*"))), \
             mock.patch.object(self.w.os, "open", lambda p, _f: opened.append(p) or 99):
            h = self.w.Haptic(0x26C19, bus)
        h.fd = None  # never close the fake fd
        return h, opened

    def test_bluetooth_prefixes_usb_report_with_f2_on_004c_node(self):
        self.node("hidraw4", "0005:000005AC:0000AAAA")      # unrelated BT device
        self.node("hidraw1", "0005:0000004C:00000324",       # A3120: no 0xF2 declared
                  bytes([0x85, 0x02, 0x81, 0x02, 0x85, 0x55, 0xB1, 0x02]))
        h, opened = self.haptic(self.w.BUS_BLUETOOTH)
        self.assertEqual(opened, ["/dev/hidraw1"])
        self.assertEqual(h.report.hex(" "), "f2 53 01 19 6c 02 00 21 2b 06 01 00 16 41 13")

    def test_usb_keeps_0x53_actuator_and_ignores_bluetooth_node(self):
        self.node("hidraw1", "0005:0000004C:00000324")
        self.node("hidraw2", "0003:000005AC:00000324", bytes([0x85, 0x02, 0x81, 0x02]))
        self.node("hidraw3", "0003:000005AC:00000324", bytes([0x85, 0x53, 0x91, 0x02]))
        h, opened = self.haptic(self.w.BUS_USB)
        self.assertEqual(opened, ["/dev/hidraw3"])
        self.assertEqual(h.report[:2], bytes([0x53, 0x01]))
        self.assertEqual(len(h.report), 14)


class OptInBoundaryTest(unittest.TestCase):
    def test_default_installer_does_not_touch_force_touch(self):
        for name in ("install-user.sh", "uninstall-user.sh", "managed-files.py",
                     "legacy-managed-files.json"):
            text = (ROOT / "packaging" / name).read_text(encoding="utf-8")
            self.assertNotIn("force-press", text, name)
            self.assertNotIn("force-touch", text, name)

    def test_watcher_never_reads_clipboard_or_text(self):
        text = WATCHER.read_text(encoding="utf-8")
        for needle in ("wl-paste", "wl-copy", "Clipboard", "--primary"):
            self.assertNotIn(needle, text)

    def test_unit_runs_installed_watcher(self):
        text = UNIT.read_text(encoding="utf-8")
        self.assertIn("ExecStart=/usr/bin/python3 %h/.local/bin/dank-force-press-watch", text)
        self.assertIn("--cmd %h/.local/bin/dank-lookup-selection", text)

    def test_selection_command_knows_force_press_source(self):
        text = (ROOT / "bin/dank-lookup-selection").read_text(encoding="utf-8")
        self.assertIn("force-press", text)

    def test_udev_rule_is_scoped_to_trackpad_hidraw(self):
        rules = [l for l in UDEV.read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.startswith("#")]
        self.assertEqual(rules, ['SUBSYSTEM=="hidraw", ATTRS{idVendor}=="05ac", '
                                 'ATTRS{idProduct}=="0324", TAG+="uaccess"',
                                 'SUBSYSTEM=="hidraw", KERNELS=="0005:004C:0324.*", '
                                 'TAG+="uaccess"'])


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.home = root / "home"
        stubs = root / "stubs"
        pylib = root / "pylib"
        for d in (self.home / ".local/bin", stubs, pylib):
            d.mkdir(parents=True)
        sel = self.home / ".local/bin/dank-lookup-selection"
        sel.write_text("#!/bin/sh\n")
        sel.chmod(0o755)
        (pylib / "evdev.py").write_text("")
        self.log = root / "systemctl.log"
        sysctl = stubs / "systemctl"
        sysctl.write_text(f'#!/bin/sh\necho "$@" >> "{self.log}"\n'
                          '[ "$2" = is-active ] && exit 3\nexit 0\n')
        sysctl.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home),
                        XDG_CONFIG_HOME=str(self.home / ".config"),
                        PATH=f"{stubs}:{os.environ['PATH']}", PYTHONPATH=str(pylib))
        self.bin = self.home / ".local/bin/dank-force-press-watch"
        self.unit = self.home / ".config/systemd/user/dank-force-press.service"

    def run_installer(self, *args):
        return subprocess.run(["bash", str(INSTALLER), *args], env=self.env,
                              capture_output=True, text=True, check=True)

    def systemctl_calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_dry_run_writes_nothing(self):
        self.run_installer("--dry-run")
        self.assertFalse(self.bin.exists())
        self.assertFalse(self.unit.exists())

    def test_install_leaves_service_disabled(self):
        out = self.run_installer().stdout
        self.assertEqual(self.bin.read_bytes(), WATCHER.read_bytes())
        self.assertEqual(self.unit.read_bytes(), UNIT.read_bytes())
        self.assertTrue(os.access(self.bin, os.X_OK))
        self.assertFalse(any("enable" in c for c in self.systemctl_calls()))
        self.assertIn("sudo install -m 644", out)

    def test_enable_and_backup_of_changed_file(self):
        self.bin.write_text("old watcher\n")
        self.run_installer("--enable")
        backups = list(self.bin.parent.glob("dank-force-press-watch.bak-*"))
        self.assertEqual([b.read_text() for b in backups], ["old watcher\n"])
        self.assertIn("--user enable dank-force-press.service", self.systemctl_calls())

    def test_uninstall_removes_only_force_touch_files(self):
        self.run_installer()
        self.run_installer("--uninstall")
        self.assertFalse(self.bin.exists())
        self.assertFalse(self.unit.exists())
        self.assertTrue((self.home / ".local/bin/dank-lookup-selection").exists())


if __name__ == "__main__":
    unittest.main()
