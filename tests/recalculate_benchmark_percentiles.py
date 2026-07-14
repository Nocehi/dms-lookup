#!/usr/bin/env python3
"""Independently recompute benchmark summaries from retained raw arrays."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from typing import Any, Iterator


ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import benchmark_lookup  # noqa: E402
import benchmark_ui  # noqa: E402


def phase_nodes(value: Any, path: str = "$") -> Iterator[tuple[str, dict[str, Any]]]:
    if isinstance(value, dict):
        if isinstance(value.get("raw_samples"), list) and isinstance(value.get("summary"), dict):
            yield path, value
        for key, child in value.items():
            yield from phase_nodes(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from phase_nodes(child, f"{path}[{index}]")


def recalculate(path: pathlib.Path) -> dict[str, Any]:
    raw_bytes = path.read_bytes()
    report = json.loads(raw_bytes)
    if not isinstance(report, dict) or report.get("schema_version") != 2:
        raise ValueError(f"{path}: benchmark report must use schema 2")
    is_ui = report.get("scope", {}).get("offscreen") is True
    summarise = benchmark_ui.summarise if is_ui else benchmark_lookup.summarise
    phases = []
    for location, node in phase_nodes(report):
        recomputed = summarise(node["raw_samples"])
        phases.append(
            {
                "path": location,
                "sample_count": len(node["raw_samples"]),
                "match": recomputed == node["summary"],
                "stored_summary": node["summary"],
                "recomputed_summary": recomputed,
            }
        )
    if not phases:
        raise ValueError(f"{path}: no raw sample phases")
    return {
        "input": path.name,
        "input_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "kind": "qml" if is_ui else "backend",
        "phases": phases,
        "all_match": all(phase["match"] for phase in phases),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()
    try:
        inputs = [recalculate(path) for path in args.inputs]
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        print(f"recalculate-benchmarks: {exc}", file=sys.stderr)
        return 1
    report = {
        "schema_version": 1,
        "kind": "benchmark_percentile_recalculation",
        "inputs": inputs,
        "all_match": all(item["all_match"] for item in inputs),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.output)
    return 0 if report["all_match"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
