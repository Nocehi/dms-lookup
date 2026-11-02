#!/usr/bin/env python3
"""Measure the isolated standalone Quickshell path with synthetic queries.

The harness installs the current tree into a temporary HOME, starts only the
standalone frontend with Qt's offscreen platform, and drives its explicit IPC
surface.  It never reads a clipboard or selection.  JSON is written to stdout
(and optionally ``--output``); a short p50/p95/p99 summary is written to stderr.

"Cold" starts a new frontend process for every sample.  The QML monotonic
timer starts when ``prepare`` accepts the explicit trigger, so
``cold_launch_to_first_qml_frame_tick_*`` adds measured process-to-IPC
readiness to that internal proxy duration. It is not compositor presentation
timing and does not claim to drop the operating-system page cache.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "packaging/install-user.sh"

DEFAULT_SCENARIOS = {
    "exact": "Linux",
    "no_hit": "dank-lookup-intentional-no-hit-7f24b9",
    "multi_dict": "dictionary",
    "long_entry": "set",
    "segmentation": "為甚麼會這樣",
}


def percentile(values: list[float], percentage: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil((percentage / 100) * len(ordered)))
    return round(ordered[rank - 1], 3)


def summarise(samples: list[dict[str, Any]]) -> dict[str, Any]:
    fields = (
        "startup_to_ipc_ms",
        "initial_ipc_roundtrip_ms",
        "lookup_ipc_roundtrip_ms",
        "prepare_to_lookup_start_ms",
        "first_qml_frame_tick_ms",
        "cold_launch_to_first_qml_frame_tick_lower_ms",
        "cold_launch_to_first_qml_frame_tick_upper_ms",
        "result_qml_frame_tick_ms",
        "backend_total_ms",
        "frontend_rss_kib",
    )
    summary: dict[str, Any] = {"samples": len(samples)}
    for field in fields:
        values = [float(sample[field]) for sample in samples if sample.get(field) is not None]
        summary[field] = {
            "p50": percentile(values, 50),
            "p95": percentile(values, 95),
            "p99": percentile(values, 99),
        }
    summary["phases"] = sorted({str(sample.get("phase")) for sample in samples})
    return summary


def parse_scenarios(values: list[str] | None) -> dict[str, str]:
    if not values:
        return dict(DEFAULT_SCENARIOS)
    scenarios: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"scenario must be NAME=QUERY: {value}")
        name, query = value.split("=", 1)
        if not name or not query:
            raise ValueError(f"scenario must have non-empty name and query: {value}")
        scenarios[name] = query
    return scenarios


def decode_return(stdout: str) -> str:
    value = stdout.strip()
    if value.startswith("Ret:"):
        value = value[4:].strip()
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return value
    return decoded if isinstance(decoded, str) else json.dumps(decoded, ensure_ascii=False)


class Frontend:
    def __init__(
        self,
        env: dict[str, str],
        quickshell: str,
        timeout: float,
    ) -> None:
        self.env = env
        self.quickshell = quickshell
        self.timeout = timeout
        self.process: subprocess.Popen[bytes] | None = None
        self.started_ns = 0
        self.ready_ns = 0
        self.stderr: tempfile._TemporaryFileWrapper[bytes] | None = None

    def start(self) -> None:
        self.stderr = tempfile.NamedTemporaryFile(prefix="dank-lookup-ui-", suffix=".log")
        self.started_ns = time.monotonic_ns()
        self.process = subprocess.Popen(
            [self.quickshell, "-c", "dank-lookup", "--allow-duplicate", "--no-color"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=self.stderr,
            env=self.env,
        )
        deadline = time.monotonic() + self.timeout
        last_probe = "no IPC probe completed"
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"Quickshell exited during startup: {self._log_tail()}")
            completed = self._ipc("show", check=False)
            if completed.returncode == 0:
                self.ready_ns = time.monotonic_ns()
                return
            last_probe = (
                f"exit={completed.returncode} stdout={completed.stdout.strip()[-300:]!r} "
                f"stderr={completed.stderr.strip()[-300:]!r}"
            )
            time.sleep(0.01)
        raise RuntimeError(
            f"timed out waiting for Quickshell IPC ({last_probe}); "
            f"Quickshell log: {self._log_tail()}"
        )

    def _base(self) -> list[str]:
        if self.process is None:
            raise RuntimeError("frontend is not running")
        return [self.quickshell, "ipc", "--pid", str(self.process.pid)]

    def _ipc(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            [*self._base(), *arguments],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=self.timeout,
            check=False,
        )
        if check and completed.returncode != 0:
            raise RuntimeError(
                f"IPC {' '.join(arguments)} failed: "
                f"{completed.stderr.strip() or completed.stdout.strip()}"
            )
        return completed

    def call(self, function: str, *arguments: str) -> str:
        completed = self._ipc("call", "dank-lookup", function, *arguments)
        return decode_return(completed.stdout)

    def sample(self, query: str, cold: bool, ipc_flow: str) -> dict[str, Any]:
        prepare_started_ns = time.monotonic_ns()
        query_fifo = pathlib.Path(self.env["DANK_LOOKUP_BENCHMARK_QUERY_FIFO"])

        def feed_capture() -> None:
            with query_fifo.open("w", encoding="utf-8") as handle:
                handle.write(query + "\n")

        writer = threading.Thread(target=feed_capture, daemon=True)
        writer.start()
        correlation_id = self.call("prepare", "benchmark")
        prepare_returned_ns = time.monotonic_ns()
        lookup_started_ns = time.monotonic_ns()
        returned = self.call("capturePrepared", correlation_id, "benchmark")
        lookup_returned_ns = time.monotonic_ns()
        if returned not in {correlation_id, ""}:
            raise RuntimeError(f"capturePrepared rejected request: {returned}")
        writer.join(timeout=self.timeout)
        if writer.is_alive():
            raise RuntimeError("selection capture did not consume benchmark query socket")
        lookup_ms: float | None = round(
            (lookup_returned_ns - lookup_started_ns) / 1_000_000, 3
        )

        deadline = time.monotonic() + self.timeout
        snapshot: dict[str, Any] = {}
        while time.monotonic() < deadline:
            raw = self.call("benchmarkState")
            try:
                snapshot = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"invalid benchmarkState JSON: {raw[:300]}") from exc
            if (
                float(snapshot.get("first_qml_frame_tick_ns", -1)) >= 0
                and float(snapshot.get("result_qml_frame_tick_ns", -1)) >= 0
            ):
                break
            if self.process is not None and self.process.poll() is not None:
                raise RuntimeError(f"Quickshell exited during lookup: {self._log_tail()}")
            # Each `quickshell ipc` probe is a separate client process and its
            # handler runs on the UI side. Tight polling would itself contend
            # with the frame loop being measured.
            time.sleep(0.05)
        else:
            raise RuntimeError(f"timed out waiting for rendered result: {self._log_tail()}")

        first_ms = round(float(snapshot["first_qml_frame_tick_ns"]) / 1_000_000, 3)
        result_ms = round(float(snapshot["result_qml_frame_tick_ns"]) / 1_000_000, 3)
        startup_ms = round((self.ready_ns - self.started_ns) / 1_000_000, 3) if cold else None
        prepare_ms = round((prepare_returned_ns - prepare_started_ns) / 1_000_000, 3)
        qml_lookup_started = snapshot.get("lookup_started_ns")
        qml_lookup_ms = (
            round(float(qml_lookup_started) / 1_000_000, 3)
            if qml_lookup_started is not None and float(qml_lookup_started) >= 0
            else None
        )
        return {
            "phase": snapshot.get("phase"),
            "startup_to_ipc_ms": startup_ms,
            "initial_ipc_roundtrip_ms": prepare_ms,
            "lookup_ipc_roundtrip_ms": lookup_ms,
            "prepare_to_lookup_start_ms": qml_lookup_ms,
            "first_qml_frame_tick_ms": first_ms,
            "cold_launch_to_first_qml_frame_tick_lower_ms": (
                round(startup_ms + first_ms, 3) if startup_ms is not None else None
            ),
            "cold_launch_to_first_qml_frame_tick_upper_ms": (
                round(startup_ms + prepare_ms + first_ms, 3)
                if startup_ms is not None else None
            ),
            "result_qml_frame_tick_ms": result_ms,
            "offscreen": snapshot.get("offscreen") is True,
            "proxy": snapshot.get("proxy") is True,
            "backend_total_ms": snapshot.get("backend_total_ms"),
            "frontend_rss_kib": self.rss_kib(),
        }

    def rss_kib(self) -> int | None:
        if self.process is None:
            return None
        try:
            for line in pathlib.Path(f"/proc/{self.process.pid}/status").read_text(
                encoding="utf-8"
            ).splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
        except (OSError, ValueError, IndexError):
            return None
        return None

    def _log_tail(self) -> str:
        if self.stderr is None:
            return "no log"
        self.stderr.flush()
        self.stderr.seek(0)
        return self.stderr.read().decode("utf-8", errors="replace")[-1000:].strip()

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        if self.stderr is not None:
            self.stderr.close()


def install_staged(home: pathlib.Path, env: dict[str, str]) -> None:
    completed = subprocess.run(
        ["bash", str(INSTALLER), "--without-tokenizer"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"temporary standalone install failed: {completed.stderr[-1000:]}")


def run(args: argparse.Namespace) -> dict[str, Any]:
    quickshell = shutil.which("quickshell")
    if quickshell is None:
        raise RuntimeError("quickshell is not installed")
    scenarios = parse_scenarios(args.scenario)
    report: dict[str, Any] = {
        "schema_version": 2,
        "scope": {
            "measured": "explicit IPC trigger to offscreen QML frame-loop tick proxies",
            "offscreen": True,
            "proxy": True,
            "visible_or_presented_frame": False,
            "selection_capture": "synthetic private FIFO capture proxy; no clipboard or primary selection is read",
            "cold_definition": "new Quickshell process per sample; OS page cache is not dropped",
            "warm_definition": "same resident standalone UI process; every lookup remains explicitly triggered",
            "privacy": "scenario labels only in report; synthetic query text is not emitted",
        },
        "parameters": {
            "cold_samples": args.cold_samples,
            "warm_samples": args.warm_samples,
            "scenario_names": list(scenarios),
            "config_supplied": args.config is not None,
            "platform": "offscreen",
            "ipc_flow": args.ipc_flow,
            "cold_launch_bound": "lower excludes initial IPC; upper includes its full round trip",
        },
        "environment": {
            "qt_qpa_platform": "offscreen",
            "wayland_display_present": bool(os.environ.get("WAYLAND_DISPLAY")),
            "xdg_session_type": os.environ.get("XDG_SESSION_TYPE"),
            "desktop": os.environ.get("XDG_CURRENT_DESKTOP"),
            "python": sys.version.split()[0],
            "quickshell": quickshell,
        },
        "clock": {
            "name": "time.monotonic_ns",
            "implementation": time.get_clock_info("monotonic").implementation,
            "resolution_seconds": time.get_clock_info("monotonic").resolution,
            "monotonic": time.get_clock_info("monotonic").monotonic,
            "adjustable": time.get_clock_info("monotonic").adjustable,
        },
        "scenarios": {},
    }

    with tempfile.TemporaryDirectory(prefix="dank-lookup-ui-benchmark-") as temporary:
        root = pathlib.Path(temporary)
        home = root / "home"
        runtime = root / "runtime"
        home.mkdir(mode=0o700)
        runtime.mkdir(mode=0o700)
        query_fifo = root / "query.fifo"
        os.mkfifo(query_fifo, 0o600)
        selection_capture = root / "benchmark-selection-capture"
        selection_capture.write_text(
            """#!/bin/sh
set -eu
IFS= read -r query < "$DANK_LOOKUP_BENCHMARK_QUERY_FIFO" || true
printf '%s\\n%s\\n%s\\n%s\\n%s\\n%s' \\
    'capture-v1' 'success_nonempty' 'primary' 'success_nonempty' \\
    'not_attempted' "$query"
""",
            encoding="utf-8",
        )
        selection_capture.chmod(0o700)
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_DATA_HOME": str(home / ".local/share"),
                "XDG_RUNTIME_DIR": str(runtime),
                "QT_QPA_PLATFORM": "offscreen",
                "DANK_LOOKUP_BENCHMARK": "1",
                "DANK_LOOKUP_COMMAND": str(home / ".local/bin/dank-lookup"),
                "DANK_LOOKUP_SELECTION_COMMAND": str(selection_capture),
                "DANK_LOOKUP_BENCHMARK_QUERY_FIFO": str(query_fifo),
                "PYTHONDONTWRITEBYTECODE": "1",
                "NO_COLOR": "1",
            }
        )
        if args.config:
            env["DANK_LOOKUP_CONFIG"] = str(args.config)
        install_staged(home, env)

        for name, query in scenarios.items():
            cold: list[dict[str, Any]] = []
            cold_failures: list[dict[str, Any]] = []
            for index in range(args.cold_samples):
                frontend = Frontend(env, quickshell, args.timeout)
                try:
                    frontend.start()
                    sample = frontend.sample(query, cold=True, ipc_flow=args.ipc_flow)
                    sample.update({"sample_index": index, "sample_phase": "cold"})
                    cold.append(sample)
                except Exception as exc:
                    message = str(exc).replace(query, "[query]")[:500]
                    cold_failures.append(
                        {
                            "sample_index": index,
                            "sample_phase": "cold",
                            "error_type": type(exc).__name__,
                            "message": message,
                            "timeout": "timeout" in message.casefold()
                            or "timed out" in message.casefold(),
                        }
                    )
                finally:
                    frontend.stop()

            warm: list[dict[str, Any]] = []
            warm_failures: list[dict[str, Any]] = []
            frontend = Frontend(env, quickshell, args.timeout)
            try:
                frontend.start()
                for index in range(args.warm_samples):
                    try:
                        sample = frontend.sample(query, cold=False, ipc_flow=args.ipc_flow)
                        sample.update({"sample_index": index, "sample_phase": "warm"})
                        warm.append(sample)
                    except Exception as exc:
                        message = str(exc).replace(query, "[query]")[:500]
                        warm_failures.append(
                            {
                                "sample_index": index,
                                "sample_phase": "warm",
                                "error_type": type(exc).__name__,
                                "message": message,
                                "timeout": "timeout" in message.casefold()
                                or "timed out" in message.casefold(),
                            }
                        )
            except Exception as exc:
                message = str(exc).replace(query, "[query]")[:500]
                warm_failures.append(
                    {
                        "sample_index": None,
                        "sample_phase": "warm_startup",
                        "error_type": type(exc).__name__,
                        "message": message,
                        "timeout": "timeout" in message.casefold()
                        or "timed out" in message.casefold(),
                    }
                )
            finally:
                frontend.stop()
            report["scenarios"][name] = {
                "cold": {
                    "raw_samples": cold,
                    "failures": cold_failures,
                    "summary": summarise(cold),
                },
                "warm": {
                    "raw_samples": warm,
                    "failures": warm_failures,
                    "summary": summarise(warm),
                },
            }
    return report


def print_human(report: dict[str, Any]) -> None:
    print("Dank Lookup standalone UI benchmark (synthetic input; no clipboard read)", file=sys.stderr)
    for name, phases in report["scenarios"].items():
        for phase in ("cold", "warm"):
            values = phases[phase]["summary"]
            first = values["first_qml_frame_tick_ms"]
            result = values["result_qml_frame_tick_ms"]
            print(
                f"  {name:12s} {phase:4s} n={values['samples']:2d} "
                f"first_qml_tick p50={first['p50']}ms p95={first['p95']}ms "
                f"result_qml_tick p50={result['p50']}ms p95={result['p95']}ms",
                file=sys.stderr,
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=pathlib.Path)
    parser.add_argument("--scenario", action="append", help="NAME=QUERY (repeatable)")
    parser.add_argument("--cold-samples", type=int, default=2)
    parser.add_argument("--warm-samples", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--ipc-flow",
        choices=("capture-transport",),
        default="capture-transport",
        help="metadata IPC, private capture stream, and fixed backend transport",
    )
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args(argv)
    if args.cold_samples < 1 or args.warm_samples < 1:
        parser.error("sample counts must be positive")
    report = run(args)
    print_human(report)
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    sys.stdout.write(encoded)
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
