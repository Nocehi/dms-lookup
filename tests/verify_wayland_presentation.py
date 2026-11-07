#!/usr/bin/env python3
"""Record owner-observed loading/result presentation in a real Wayland session."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys
import time
from typing import Mapping

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from source_identity import INSTALLED_SOURCE, installed_source  # noqa: E402


class WaylandVerificationError(RuntimeError):
    pass


def validate_wayland_environment(
    environment: Mapping[str, str],
    *,
    require_socket: bool = True,
) -> dict[str, object]:
    platform = str(environment.get("QT_QPA_PLATFORM", "")).casefold()
    if "offscreen" in platform:
        raise WaylandVerificationError("offscreen Qt platform is not presentation evidence")
    if str(environment.get("XDG_SESSION_TYPE", "")).casefold() != "wayland":
        raise WaylandVerificationError("XDG_SESSION_TYPE is not wayland")
    display = str(environment.get("WAYLAND_DISPLAY", ""))
    runtime = str(environment.get("XDG_RUNTIME_DIR", ""))
    if not display or not runtime:
        raise WaylandVerificationError("Wayland display metadata is incomplete")
    socket_path = pathlib.Path(runtime) / display
    if require_socket and not socket_path.is_socket():
        raise WaylandVerificationError("Wayland display socket is unavailable")
    return {
        "qt_qpa_platform": platform or None,
        "xdg_session_type": "wayland",
        "wayland_display": display,
        "desktop": environment.get("XDG_CURRENT_DESKTOP"),
        "compositor": environment.get("XDG_SESSION_DESKTOP"),
    }


def build_record(
    *,
    owner: str,
    loading_pass: bool,
    result_pass: bool,
    environment: Mapping[str, str],
    timestamp_source: str | None = None,
    trigger_ns: int | None = None,
    loading_presented_ns: int | None = None,
    result_presented_ns: int | None = None,
    require_socket: bool = True,
    tested_source: dict[str, object] | None = None,
) -> dict[str, object]:
    environment_record = validate_wayland_environment(
        environment,
        require_socket=require_socket,
    )
    provided = (trigger_ns, loading_presented_ns, result_presented_ns)
    if timestamp_source is None and any(value is not None for value in provided):
        raise WaylandVerificationError("presentation timestamps require an evidence source")
    if timestamp_source is not None and not all(value is not None for value in provided):
        raise WaylandVerificationError("presentation timestamp evidence is incomplete")
    record: dict[str, object] = {
        "schema_version": 1,
        "kind": "real_wayland_owner_presentation_check",
        "recorded_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "owner": owner,
        "environment": environment_record,
        "offscreen": False,
        "owner_checks": {
            "loading_presented": loading_pass,
            "result_presented": result_pass,
            "pass": loading_pass and result_pass,
        },
        "tested_source": tested_source,
        "presentation_timestamp_evidence": timestamp_source is not None,
        "clock": {
            "recorder": "time.monotonic_ns",
            "implementation": time.get_clock_info("monotonic").implementation,
        },
    }
    if timestamp_source is not None:
        assert trigger_ns is not None
        assert loading_presented_ns is not None
        assert result_presented_ns is not None
        if not (trigger_ns <= loading_presented_ns <= result_presented_ns):
            raise WaylandVerificationError("presentation timestamps are not monotonic")
        record["presentation_timestamps"] = {
            "source": timestamp_source,
            "trigger_ns": trigger_ns,
            "loading_presented_ns": loading_presented_ns,
            "result_presented_ns": result_presented_ns,
            "loading_presentation_ms": round(
                (loading_presented_ns - trigger_ns) / 1_000_000,
                3,
            ),
            "result_presentation_ms": round(
                (result_presented_ns - trigger_ns) / 1_000_000,
                3,
            ),
        }
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--loading", choices=("pass", "fail"), required=True)
    parser.add_argument("--result", choices=("pass", "fail"), required=True)
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--compositor-timestamp-source")
    parser.add_argument("--trigger-ns", type=int)
    parser.add_argument("--loading-presented-ns", type=int)
    parser.add_argument("--result-presented-ns", type=int)
    parser.add_argument(
        "--installed-source",
        type=pathlib.Path,
        default=INSTALLED_SOURCE,
        help="written by install-user.sh; names the commit of the installed card",
    )
    args = parser.parse_args(argv)
    try:
        record = build_record(
            owner=args.owner,
            loading_pass=args.loading == "pass",
            result_pass=args.result == "pass",
            environment=os.environ,
            timestamp_source=args.compositor_timestamp_source,
            trigger_ns=args.trigger_ns,
            loading_presented_ns=args.loading_presented_ns,
            result_presented_ns=args.result_presented_ns,
            tested_source=installed_source(args.installed_source),
        )
    except WaylandVerificationError as exc:
        print(f"WAYLAND_PRESENTATION: FAIL: {exc}", file=sys.stderr)
        return 2
    encoded = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    sys.stdout.write(encoded)
    if args.output is not None:
        args.output.write_text(encoded, encoding="utf-8")
    return 0 if record["owner_checks"]["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
