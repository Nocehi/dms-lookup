#!/usr/bin/env python3
"""Collect privacy-preserving Wayland selection latency samples.

Run this tool while the named real application owns the requested selection.
It never stores selection content or a content hash: only duration, exit/timeout,
byte count, whitespace classification, owner class, and session metadata.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from source_identity import checkout_source  # noqa: E402


SCHEMA_VERSION = 1
SCENARIOS = (
    "primary-present",
    "clipboard-present",
    "primary-absent",
    "clipboard-empty",
    "owner-exit",
)


def default_command(selection: str) -> list[str]:
    command = ["wl-paste", "--no-newline", "--type", "text"]
    if selection == "primary":
        command.append("--primary")
    return command


def classify(returncode: int | None, timed_out: bool, payload: bytes) -> str:
    if timed_out:
        return "timeout"
    if returncode != 0:
        return "command_error"
    if not payload:
        return "success_empty"
    if not payload.strip():
        return "success_whitespace"
    return "success_nonempty"


def measure_once(command: list[str], timeout_ms: int, index: int) -> dict[str, Any]:
    started_ns = time.monotonic_ns()
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_ms / 1000)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        stdout, stderr = process.communicate()
    duration_ms = round((time.monotonic_ns() - started_ns) / 1_000_000, 3)
    return {
        "index": index,
        "duration_ms": duration_ms,
        "returncode": process.returncode,
        "timed_out": timed_out,
        "byte_count": len(stdout),
        "whitespace_only": bool(stdout) and not stdout.strip(),
        "stderr_byte_count": len(stderr),
        "outcome": classify(process.returncode, timed_out, stdout),
    }


def atomic_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def collect(args: argparse.Namespace) -> dict[str, Any]:
    command = args.command or default_command(args.selection)
    samples = [measure_once(command, args.probe_timeout_ms, index) for index in range(args.samples)]
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "selection_latency_raw",
        "collected_at": dt.datetime.now(dt.UTC).isoformat(),
        "owner_class": args.owner_class,
        "application": args.application,
        "toolkit": args.toolkit,
        "selection": args.selection,
        "scenario": args.scenario,
        "probe_timeout_ms": args.probe_timeout_ms,
        "command_metadata": {
            "executable": pathlib.Path(command[0]).name,
            "argument_count": len(command) - 1,
        },
        "wayland": {
            "wayland_display": os.environ.get("WAYLAND_DISPLAY"),
            "xdg_session_type": os.environ.get("XDG_SESSION_TYPE"),
            "xdg_current_desktop": os.environ.get("XDG_CURRENT_DESKTOP"),
            "desktop_session": os.environ.get("DESKTOP_SESSION"),
            "compositor": args.compositor,
        },
        "privacy": {
            "content_stored": False,
            "content_hash_stored": False,
        },
        "tested_source": checkout_source(pathlib.Path(__file__).resolve().parent.parent),
        "samples": samples,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-class", required=True)
    parser.add_argument("--application", required=True)
    parser.add_argument("--toolkit", required=True)
    parser.add_argument("--compositor", required=True)
    parser.add_argument("--selection", choices=("primary", "clipboard"), required=True)
    parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    parser.add_argument("--samples", type=int, required=True)
    parser.add_argument("--probe-timeout-ms", type=int, default=5000)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument(
        "--command",
        nargs=argparse.REMAINDER,
        help="override capture command for fixture validation; must be the final option",
    )
    args = parser.parse_args(argv)
    if args.samples < 1:
        parser.error("--samples must be positive")
    if not 1 <= args.probe_timeout_ms <= 5000:
        parser.error("--probe-timeout-ms must be in 1..5000")
    if args.scenario.startswith("primary-") and args.selection != "primary":
        parser.error("primary scenarios require --selection primary")
    if args.scenario.startswith("clipboard-") and args.selection != "clipboard":
        parser.error("clipboard scenarios require --selection clipboard")
    if args.command == []:
        parser.error("--command requires an executable")
    report = collect(args)
    atomic_json(args.output, report)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
