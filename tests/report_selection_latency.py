#!/usr/bin/env python3
"""Generate candidate timeout trade-offs from raw selection latency files."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import pathlib
from collections import defaultdict
from typing import Any, Iterable


CANDIDATE_TIMEOUTS_MS = (250, 350, 500, 750, 1000, 1500, 2000)
PRESENT_MINIMUM = 100
EDGE_MINIMUM = 20


def percentile(values: Iterable[float], percentage: int) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    index = max(0, math.ceil((percentage / 100) * len(ordered)) - 1)
    return round(ordered[index], 3)


def load_raw(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != 1 or value.get("kind") != "selection_latency_raw":
        raise ValueError(f"{path}: unsupported raw selection schema")
    if value.get("privacy") != {"content_stored": False, "content_hash_stored": False}:
        raise ValueError(f"{path}: privacy declaration is absent or unsafe")
    samples = value.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError(f"{path}: no raw samples")
    required = {
        "duration_ms",
        "returncode",
        "timed_out",
        "byte_count",
        "whitespace_only",
        "outcome",
    }
    if any(not isinstance(sample, dict) or not required.issubset(sample) for sample in samples):
        raise ValueError(f"{path}: malformed raw sample")
    return value


def sample_failed(sample: dict[str, Any], candidate_ms: int) -> bool:
    return (
        bool(sample["timed_out"])
        or sample["returncode"] != 0
        or float(sample["duration_ms"]) > candidate_ms
    )


def distribution(samples: list[dict[str, Any]]) -> dict[str, Any]:
    durations = [float(sample["duration_ms"]) for sample in samples]
    return {
        "count": len(samples),
        "p50_ms": percentile(durations, 50),
        "p95_ms": percentile(durations, 95),
        "p99_ms": percentile(durations, 99),
        "max_observed_ms": round(max(durations), 3) if durations else None,
        "actual_timeout_count": sum(bool(sample["timed_out"]) for sample in samples),
        "actual_error_count": sum(
            sample["returncode"] != 0 and not sample["timed_out"] for sample in samples
        ),
    }


def generate(paths: list[pathlib.Path]) -> dict[str, Any]:
    documents = [(path, load_raw(path)) for path in paths]
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    completeness_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    inputs = []
    for path, document in documents:
        owner = str(document["owner_class"])
        scenario = str(document["scenario"])
        grouped[(owner, scenario)].extend(document["samples"])
        completeness_counts[owner][scenario] += len(document["samples"])
        inputs.append(
            {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "owner_class": owner,
                "scenario": scenario,
                "samples": len(document["samples"]),
            }
        )

    completeness_errors = []
    owners = sorted(completeness_counts)
    if len(owners) < 3:
        completeness_errors.append("fewer than three real owner classes")
    for owner in owners:
        counts = completeness_counts[owner]
        for scenario in ("primary-present", "clipboard-present"):
            if counts.get(scenario, 0) < PRESENT_MINIMUM:
                completeness_errors.append(
                    f"{owner}/{scenario}: {counts.get(scenario, 0)} < {PRESENT_MINIMUM}"
                )
        for scenario in ("primary-absent", "clipboard-empty", "owner-exit"):
            if counts.get(scenario, 0) < EDGE_MINIMUM:
                completeness_errors.append(
                    f"{owner}/{scenario}: {counts.get(scenario, 0)} < {EDGE_MINIMUM}"
                )

    distributions = {
        f"{owner}/{scenario}": distribution(samples)
        for (owner, scenario), samples in sorted(grouped.items())
    }
    primary_present = [
        sample
        for (owner, scenario), samples in grouped.items()
        if scenario == "primary-present"
        for sample in samples
    ]
    candidate_reports = []
    for candidate in CANDIDATE_TIMEOUTS_MS:
        all_samples = [sample for samples in grouped.values() for sample in samples]
        failed = [sample for sample in all_samples if sample_failed(sample, candidate)]
        erroneous_fallbacks = [
            sample for sample in primary_present if sample_failed(sample, candidate)
        ]
        fallback_delays = [
            min(float(sample["duration_ms"]), float(candidate)) for sample in failed
        ]
        candidate_reports.append(
            {
                "timeout_ms": candidate,
                "observed_timeout_error_rate": round(len(failed) / len(all_samples), 6)
                if all_samples
                else None,
                "erroneous_clipboard_fallback_rate": round(
                    len(erroneous_fallbacks) / len(primary_present), 6
                )
                if primary_present
                else None,
                "worst_case_fallback_delay_ms": round(max(fallback_delays), 3)
                if fallback_delays
                else 0.0,
            }
        )

    successful_primary = [
        sample
        for sample in primary_present
        if not sample["timed_out"] and sample["returncode"] == 0
    ]
    durations = [float(sample["duration_ms"]) for sample in successful_primary]
    p99 = percentile(durations, 99)
    max_observed = max(durations) if durations else None
    provisional = None
    if p99 is not None and max_observed is not None:
        provisional = min(2000, max(250, math.ceil(max(2 * p99, max_observed + 50))))

    owner_p99 = {
        owner: percentile(
            [float(sample["duration_ms"]) for sample in grouped[(owner, "primary-present")]],
            99,
        )
        for owner in owners
    }
    numeric_owner_p99 = [value for value in owner_p99.values() if value is not None]
    owner_difference = {
        "primary_present_p99_ms": owner_p99,
        "p99_spread_ms": round(max(numeric_owner_p99) - min(numeric_owner_p99), 3)
        if numeric_owner_p99
        else None,
    }
    return {
        "schema_version": 1,
        "kind": "selection_latency_tradeoff_report",
        "dataset_complete": not completeness_errors,
        "completeness_errors": completeness_errors,
        "inputs": inputs,
        "distributions": distributions,
        "candidate_timeouts": candidate_reports,
        "owner_differences": owner_difference,
        "provisional_formula": "clamp(250,2000,ceil(max(2*p99,max_observed+50)))",
        "provisional_candidate_ms": provisional,
        "production_default_approved": False,
        "approved_primary_timeout_ms": None,
        "approved_clipboard_timeout_ms": None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args(argv)
    report = generate(args.inputs)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.output)
    if args.require_complete and not report["dataset_complete"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
