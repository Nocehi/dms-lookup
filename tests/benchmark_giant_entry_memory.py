#!/usr/bin/env python3
"""Measure giant-entry RSS in isolated workers and retain correctness evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import platform
import resource
import subprocess
import sys
import time
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

from dank_lookup_core import render_entries  # noqa: E402


SIZES_MIB = (1, 8, 32)
PUBLIC_JSON_LIMIT_BYTES = 100 * 1024
CANDIDATE_RSS_MULTIPLIER = 4.0


def _digest_text(parts: list[str]) -> str:
    digest = hashlib.sha256()
    for value in parts:
        for offset in range(0, len(value), 1024 * 1024):
            digest.update(value[offset : offset + 1024 * 1024].encode("utf-8"))
    return digest.hexdigest()


def _worker(size_mib: int) -> dict[str, Any]:
    tail = "GIANT-ENTRY-COMPLETE-TAIL"
    definition_chars = size_mib * 1024 * 1024
    definition = "x" * (definition_chars - len(tail)) + tail
    prefix = "[GiantDict]\ngiant\n"
    rendered, vault = render_entries(
        [
            {
                "dict": "GiantDict",
                "word": "giant",
                "definition": definition,
                "_definition_format": "plain",
            }
        ],
        max_entry_chars=256,
        max_body_chars=512,
        max_inline_full_chars=256 * 1024,
        retain_entry_vault=True,
        retain_full_blobs=True,
    )
    reference = rendered.get("full_ref")
    full_body = vault.get("full_blobs", {}).get(reference)
    public_json_bytes = len(
        json.dumps(rendered, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    expected_digest = _digest_text([prefix, definition])
    actual_digest = _digest_text([full_body]) if isinstance(full_body, str) else None
    checks = {
        "public_json_under_100_kib": public_json_bytes < PUBLIC_JSON_LIMIT_BYTES,
        "oversized_body_not_inline": rendered.get("full_body") is None,
        "full_reference_present": isinstance(reference, str) and bool(reference),
        "full_body_present": isinstance(full_body, str),
        "full_length_complete": isinstance(full_body, str)
        and len(full_body) == len(prefix) + definition_chars,
        "full_prefix_complete": isinstance(full_body, str) and full_body.startswith(prefix),
        "full_tail_complete": isinstance(full_body, str) and full_body.endswith(tail),
        "full_hash_complete": actual_digest == expected_digest,
        "reference_matches_hash": isinstance(reference, str)
        and reference == expected_digest[:32],
        "raw_definition_not_retained": all(
            "raw_definition" not in entry
            for entry in vault.get("entries", {}).values()
        ),
    }
    return {
        "size_mib": size_mib,
        "definition_chars": definition_chars,
        "ru_maxrss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "public_json_bytes": public_json_bytes,
        "full_chars": rendered.get("full_chars"),
        "full_ref": reference,
        "checks": checks,
        "correctness_pass": all(checks.values()),
    }


def _run_parent(output: pathlib.Path | None, approved_multiplier: float | None) -> int:
    samples: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for size_mib in SIZES_MIB:
        started_ns = time.monotonic_ns()
        command = [
            sys.executable,
            os.fspath(pathlib.Path(__file__).resolve()),
            "--worker-size-mib",
            str(size_mib),
        ]
        try:
            run = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=180,
                check=False,
            )
            sample = json.loads(run.stdout) if run.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            run = None
            sample = None
            failures.append({"size_mib": size_mib, "error": type(exc).__name__})
        if not isinstance(sample, dict):
            if run is not None:
                failures.append(
                    {
                        "size_mib": size_mib,
                        "returncode": run.returncode,
                        "stderr": run.stderr[-2000:],
                    }
                )
            continue
        sample["elapsed_ms"] = round((time.monotonic_ns() - started_ns) / 1_000_000, 3)
        samples.append(sample)

    by_size = {sample["size_mib"]: sample for sample in samples}
    delta_kib = None
    if 1 in by_size and 32 in by_size:
        delta_kib = by_size[32]["ru_maxrss_kib"] - by_size[1]["ru_maxrss_kib"]
    multiplier = approved_multiplier or CANDIDATE_RSS_MULTIPLIER
    candidate_limit_kib = multiplier * 31 * 1024
    rss_candidate_pass = delta_kib is not None and delta_kib <= candidate_limit_kib
    correctness_pass = not failures and len(samples) == len(SIZES_MIB) and all(
        sample["correctness_pass"] for sample in samples
    )
    owner_approved = approved_multiplier is not None
    report = {
        "schema_version": 1,
        "benchmark": "giant_entry_memory",
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "ru_maxrss_unit": "KiB (Linux)",
        },
        "raw_samples": samples,
        "failures": failures,
        "summary": {
            "correctness_pass": correctness_pass,
            "rss_32_minus_1_kib": delta_kib,
            "rss_multiplier": multiplier,
            "rss_limit_kib": candidate_limit_kib,
            "rss_candidate_pass": rss_candidate_pass,
            "owner_approved": owner_approved,
            "rss_gate_enforced": owner_approved,
        },
    }
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
    sys.stdout.write(payload)
    if not correctness_pass:
        return 1
    if owner_approved and not rss_candidate_pass:
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-size-mib", type=int, choices=SIZES_MIB)
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument(
        "--approved-rss-multiplier",
        type=float,
        help="enforce an owner-approved multiplier; omitted means proposal-only",
    )
    args = parser.parse_args()
    if args.worker_size_mib is not None:
        sys.stdout.write(json.dumps(_worker(args.worker_size_mib)) + "\n")
        return 0
    if args.approved_rss_multiplier is not None and args.approved_rss_multiplier <= 0:
        parser.error("--approved-rss-multiplier must be positive")
    return _run_parent(args.output, args.approved_rss_multiplier)


if __name__ == "__main__":
    raise SystemExit(main())
