#!/usr/bin/env python3
"""Measure backend lookup strategies without claiming UI presentation timing.

JSON is written to stdout (and optionally --output). A concise human p50/p95
summary is written to stderr. Cold/warm meanings are stated per strategy; the
script does not and cannot drop the operating-system page cache.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import uuid


ROOT = pathlib.Path(__file__).resolve().parent.parent
LIB = ROOT / "lib"
CLI = ROOT / "bin/dank-lookup"
sys.path.insert(0, str(LIB))

from dank_lookup_core import SdcvClient, lookup  # noqa: E402
from dank_lookup_runtime import request_backend  # noqa: E402


DEFAULT_SCENARIOS = {
    "exact": "Linux",
    "no_hit": "dank-lookup-intentional-no-hit-7f24b9",
    "multi_dict": "dictionary",
    "long_entry": "set",
    "segmentation": "為甚麼會這樣",
}


def percentile(values, percentage):
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil((percentage / 100) * len(ordered)))
    return round(ordered[rank - 1], 3)


def summarise(samples):
    wall = [sample["wall_ms"] for sample in samples]
    backend = [sample["backend_ms"] for sample in samples if sample["backend_ms"] is not None]
    total = [sample["core_total_ms"] for sample in samples if sample["core_total_ms"] is not None]
    return {
        "samples": len(samples),
        "wall_ms": {
            "p50": percentile(wall, 50),
            "p95": percentile(wall, 95),
            "p99": percentile(wall, 99),
        },
        "backend_ms": {
            "p50": percentile(backend, 50),
            "p95": percentile(backend, 95),
            "p99": percentile(backend, 99),
        },
        "core_total_ms": {
            "p50": percentile(total, 50),
            "p95": percentile(total, 95),
            "p99": percentile(total, 99),
        },
        "response_bytes": {
            "min": min((sample["response_bytes"] for sample in samples), default=0),
            "max": max((sample["response_bytes"] for sample in samples), default=0),
        },
    }


def sample_from(started_ns, result):
    encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
    full_sizes = [
        int(mode.get("full_chars", len(mode.get("full_body") or "")))
        for mode in result.get("results_by_mode", {}).values()
        if isinstance(mode, dict)
    ]
    timing = result.get("timing", {})
    return {
        "wall_ms": round((time.monotonic_ns() - started_ns) / 1_000_000, 3),
        "backend_ms": timing.get("backend_ms"),
        "core_total_ms": timing.get("total_ms"),
        "response_bytes": len(encoded),
        "exact_modes": len(result.get("available_modes", [])),
        "max_full_body_chars": max(full_sizes, default=0),
        "error_code": (result.get("error") or {}).get("code") if isinstance(result.get("error"), dict) else None,
    }


def one_shot(query, config, env):
    command = [
        sys.executable,
        str(CLI),
        "lookup",
        "--request-id",
        str(uuid.uuid4()),
        "--sequence",
        "1",
    ]
    if config:
        command += ["--config", str(config)]
    started = time.monotonic_ns()
    completed = subprocess.run(
        command,
        input=query.encode("utf-8"),
        capture_output=True,
        env=env,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.decode("utf-8", errors="replace"))
    result = json.loads(completed.stdout)
    return sample_from(started, result)


def resident(query, config, client):
    started = time.monotonic_ns()
    result, _ = lookup(query, config_path=config, request_id="benchmark", sequence=1, client=client)
    return sample_from(started, result)


def wait_for_socket(path, process, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_socket():
            return
        if process.poll() is not None:
            detail = process.stderr.read().decode("utf-8", errors="replace").strip() if process.stderr else ""
            raise RuntimeError(f"socket server exited before becoming ready: {detail[-300:]}")
        time.sleep(0.005)
    raise RuntimeError("timed out waiting for socket server")


def start_server(config, env, idle_timeout):
    command = [sys.executable, str(CLI), "backend-serve", "--idle-timeout", str(idle_timeout)]
    if config:
        command += ["--config", str(config)]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env)
    path = pathlib.Path(env["XDG_RUNTIME_DIR"]) / "dank-lookup/backend.sock"
    wait_for_socket(path, process)
    return process, path


def socket_lookup(query, config, path):
    request = {
        "action": "lookup",
        "query": query,
        "request_id": str(uuid.uuid4()),
        "sequence": 1,
    }
    if config:
        request["config"] = str(config)
    started = time.monotonic_ns()
    result = request_backend(request, timeout=30, path=path)
    return sample_from(started, result)


def parse_scenarios(values):
    if not values:
        return dict(DEFAULT_SCENARIOS)
    scenarios = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"scenario must be NAME=QUERY: {value}")
        name, query = value.split("=", 1)
        scenarios[name] = query
    return scenarios


def run(args):
    scenarios = parse_scenarios(args.scenario)
    strategies = [item.strip() for item in args.strategies.split(",") if item.strip()]
    unknown = set(strategies) - {"one-shot", "resident", "socket"}
    if unknown:
        raise ValueError(f"unknown strategies: {sorted(unknown)}")
    env = os.environ.copy()
    env.update({"DANK_LOOKUP_LIBDIR": str(LIB), "PYTHONDONTWRITEBYTECODE": "1"})
    if args.config:
        env["DANK_LOOKUP_CONFIG"] = str(args.config)

    report = {
        "schema_version": 2,
        "scope": {
            "measured": "backend request to parsed JSON result",
            "ui_qml_frame_tick": "not measured",
            "visible_or_presented_frame": "not measured",
            "cold_definition": "strategy-specific; see each strategy phase_definitions",
            "privacy": "explicit synthetic queries only; no clipboard read, network, telemetry, history, or default runtime retention",
            "query_output": "scenario labels only; synthetic query strings are omitted",
        },
        "parameters": {
            "cold_samples": args.cold_samples,
            "warm_samples": args.warm_samples,
            "socket_idle_timeout_s": args.socket_idle_timeout,
            "scenario_names": list(scenarios),
        },
        "environment": {
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "xdg_session_type": os.environ.get("XDG_SESSION_TYPE"),
            "desktop": os.environ.get("XDG_CURRENT_DESKTOP"),
        },
        "clock": {
            "name": "time.monotonic_ns",
            "implementation": time.get_clock_info("monotonic").implementation,
            "resolution_seconds": time.get_clock_info("monotonic").resolution,
            "monotonic": time.get_clock_info("monotonic").monotonic,
            "adjustable": time.get_clock_info("monotonic").adjustable,
        },
        "strategies": {},
    }

    with tempfile.TemporaryDirectory(prefix="dank-lookup-benchmark-") as temporary:
        runtime_base = pathlib.Path(temporary) / "runtime"
        runtime_base.mkdir(mode=0o700)
        env["XDG_RUNTIME_DIR"] = str(runtime_base)
        for strategy in strategies:
            strategy_report = {
                "model": {
                    "one-shot": "new Python process per explicit lookup",
                    "resident": "same in-process core and dictionary-list cache; benchmark-only",
                    "socket": "private 0600 Unix socket; explicit requests only; server exits on idle timeout",
                }[strategy],
                "phase_definitions": {
                    "one-shot": {
                        "cold": "first repeated samples; every sample includes a fresh Python process",
                        "warm": "later repeated samples; process is still fresh, only ambient OS caches may be warmer",
                    },
                    "resident": {
                        "cold": "same benchmark process with a new SdcvClient and empty dictionary-list cache per sample",
                        "warm": "same benchmark process and reused SdcvClient dictionary-list cache",
                    },
                    "socket": {
                        "cold": "new idle-exit server per sample, timed after its socket is ready; activation startup is excluded",
                        "warm": "same ready server and cached dictionary list across requests",
                    },
                }[strategy],
                "production_default": strategy == "one-shot",
                "scenarios": {},
                "failures": [],
            }
            socket_idle_exit_verified = None
            process = None
            try:
                for scenario, query in scenarios.items():
                    cold = []
                    warm = []
                    current_phase = "cold"
                    if strategy == "one-shot":
                        for index in range(args.cold_samples):
                            sample = one_shot(query, args.config, env)
                            sample.update({"sample_index": index, "sample_phase": "cold"})
                            cold.append(sample)
                        current_phase = "warm"
                        for index in range(args.warm_samples):
                            sample = one_shot(query, args.config, env)
                            sample.update({"sample_index": index, "sample_phase": "warm"})
                            warm.append(sample)
                    elif strategy == "resident":
                        for index in range(args.cold_samples):
                            sample = resident(query, args.config, SdcvClient())
                            sample.update({"sample_index": index, "sample_phase": "cold"})
                            cold.append(sample)
                        client = SdcvClient()
                        current_phase = "warm"
                        for index in range(args.warm_samples):
                            sample = resident(query, args.config, client)
                            sample.update({"sample_index": index, "sample_phase": "warm"})
                            warm.append(sample)
                    else:
                        for index in range(args.cold_samples):
                            process, path = start_server(args.config, env, args.socket_idle_timeout)
                            sample = socket_lookup(query, args.config, path)
                            sample.update({"sample_index": index, "sample_phase": "cold"})
                            cold.append(sample)
                            process.wait(timeout=args.socket_idle_timeout + 2)
                        process, path = start_server(args.config, env, args.socket_idle_timeout)
                        current_phase = "warm"
                        for index in range(args.warm_samples):
                            sample = socket_lookup(query, args.config, path)
                            sample.update({"sample_index": index, "sample_phase": "warm"})
                            warm.append(sample)
                        process.wait(timeout=args.socket_idle_timeout + 2)
                        socket_idle_exit_verified = process.returncode == 0 and not path.exists()
                    strategy_report["scenarios"][scenario] = {
                        "cold": {
                            "raw_samples": cold,
                            "failures": [],
                            "summary": summarise(cold),
                        },
                        "warm": {
                            "raw_samples": warm,
                            "failures": [],
                            "summary": summarise(warm),
                        },
                    }
                strategy_report["status"] = "measured"
            except (OSError, RuntimeError, subprocess.SubprocessError, TimeoutError) as exc:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                strategy_report["status"] = "unavailable"
                message = str(exc)
                for private_query in scenarios.values():
                    message = message.replace(private_query, "[query]")
                failure = {
                    "error_type": type(exc).__name__,
                    "message": message[:500],
                    "timeout": isinstance(exc, (TimeoutError, subprocess.TimeoutExpired))
                    or "timeout" in message.casefold()
                    or "timed out" in message.casefold(),
                }
                strategy_report["failures"].append(failure)
                if "scenario" in locals() and "cold" in locals() and "warm" in locals():
                    strategy_report["scenarios"][scenario] = {
                        "cold": {
                            "raw_samples": cold,
                            "failures": [failure] if current_phase == "cold" else [],
                            "summary": summarise(cold),
                        },
                        "warm": {
                            "raw_samples": warm,
                            "failures": [failure] if current_phase == "warm" else [],
                            "summary": summarise(warm),
                        },
                    }
                strategy_report["unavailable_reason"] = (
                    f"{failure['error_type']}: {failure['message']}"
                )
            if strategy == "socket":
                strategy_report["idle_exit_verified"] = socket_idle_exit_verified
            report["strategies"][strategy] = strategy_report
    return report


def print_human(report):
    print(
        "Dank Lookup backend benchmark (QML frame ticks and presentation not measured)",
        file=sys.stderr,
    )
    for strategy, strategy_data in report["strategies"].items():
        print(f"\n{strategy}: {strategy_data['model']}", file=sys.stderr)
        if strategy_data.get("status") != "measured":
            print(f"  unavailable: {strategy_data.get('unavailable_reason', 'unknown reason')}", file=sys.stderr)
            continue
        for scenario, phases in strategy_data["scenarios"].items():
            for phase in ("cold", "warm"):
                values = phases[phase]["summary"]
                print(
                    f"  {scenario:12s} {phase:4s} n={values['samples']:2d} "
                    f"wall p50={values['wall_ms']['p50']}ms p95={values['wall_ms']['p95']}ms",
                    file=sys.stderr,
                )
        if strategy == "socket":
            print(f"  idle exit verified: {strategy_data['idle_exit_verified']}", file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=pathlib.Path)
    parser.add_argument("--scenario", action="append", help="NAME=QUERY (repeatable)")
    parser.add_argument("--strategies", default="one-shot,resident,socket")
    parser.add_argument("--cold-samples", type=int, default=2)
    parser.add_argument("--warm-samples", type=int, default=8)
    parser.add_argument("--socket-idle-timeout", type=float, default=0.25)
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
