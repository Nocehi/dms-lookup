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

# Entry sizes are UTF-8 bytes, as in the dictionary file. CPython stores a str
# at the width of its widest character, so the same file size costs 1x
# (ASCII), about 0.7x (pure CJK: 3 UTF-8 bytes, 2 in memory), 2x (ASCII with
# one BMP character such as IPA) or 4x (ASCII with one astral character) in
# memory. Each kind is measured.
TEXT_KINDS = ("ascii", "cjk", "ascii_ipa", "ascii_astral")

THRESHOLD_DEFINITION = (
    "rss_32_minus_1_kib <= multiplier * str_32_minus_1_kib for every text kind, "
    "where str_*_kib is sys.getsizeof of the entry text in CPython"
)
PREVIOUS_THRESHOLD_DEFINITION = (
    "until 2026-11-07: rss_32_minus_1_kib <= multiplier * 31 MiB of UTF-8 input, "
    "ASCII only; changed because CPython stores mixed-width text at 2-4x its "
    "UTF-8 size, which no code change can avoid while the text is a str. "
    "The UTF-8-based ratio is still recorded per kind as utf8_ratio, not judged."
)


def _make_definition(kind: str, size_bytes: int, tail: str) -> str:
    """Text of ``size_bytes`` UTF-8 bytes ending with ``tail``."""

    body_bytes = size_bytes - len(tail)
    if kind == "ascii":
        body = "x" * body_bytes
    elif kind == "cjk":
        body = "漢" * (body_bytes // 3) + "x" * (body_bytes % 3)
    elif kind == "ascii_ipa":
        body = "ə" + "x" * (body_bytes - len("ə".encode("utf-8")))
    elif kind == "ascii_astral":
        body = "𠀀" + "x" * (body_bytes - len("𠀀".encode("utf-8")))
    else:
        raise ValueError(f"unknown text kind {kind!r}")
    definition = body + tail
    # Count UTF-8 bytes in chunks: encoding the whole text at once would put a
    # full UTF-8 copy next to the str and inflate the measured peak RSS.
    assert _utf8_size(definition) == size_bytes
    return definition


def _utf8_size(value: str) -> int:
    step = 1024 * 1024
    return sum(
        len(value[offset : offset + step].encode("utf-8"))
        for offset in range(0, len(value), step)
    )


def _digest_text(parts: list[str]) -> str:
    digest = hashlib.sha256()
    for value in parts:
        for offset in range(0, len(value), 1024 * 1024):
            digest.update(value[offset : offset + 1024 * 1024].encode("utf-8"))
    return digest.hexdigest()


def _worker(size_mib: int, kind: str = "ascii") -> dict[str, Any]:
    tail = "GIANT-ENTRY-COMPLETE-TAIL"
    definition = _make_definition(kind, size_mib * 1024 * 1024, tail)
    definition_chars = len(definition)
    str_bytes = sys.getsizeof(definition)
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
        "kind": kind,
        "utf8_bytes": size_mib * 1024 * 1024,
        "str_bytes": str_bytes,
        "definition_chars": definition_chars,
        "ru_maxrss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "public_json_bytes": public_json_bytes,
        "full_chars": rendered.get("full_chars"),
        "full_ref": reference,
        "checks": checks,
        "correctness_pass": all(checks.values()),
    }


def _run_worker(size_mib: int, kind: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    started_ns = time.monotonic_ns()
    command = [
        sys.executable,
        os.fspath(pathlib.Path(__file__).resolve()),
        "--worker-size-mib",
        str(size_mib),
        "--worker-kind",
        kind,
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
        return None, {"size_mib": size_mib, "kind": kind, "error": type(exc).__name__}
    if not isinstance(sample, dict):
        return None, {
            "size_mib": size_mib,
            "kind": kind,
            "returncode": run.returncode,
            "stderr": run.stderr[-2000:],
        }
    sample["elapsed_ms"] = round((time.monotonic_ns() - started_ns) / 1_000_000, 3)
    return sample, None


def _kind_summary(by_size: dict[int, dict[str, Any]], multiplier: float) -> dict[str, Any]:
    if 1 not in by_size or 32 not in by_size:
        return {"complete": False, "rss_pass": False}
    rss_delta_kib = by_size[32]["ru_maxrss_kib"] - by_size[1]["ru_maxrss_kib"]
    str_delta_kib = (by_size[32]["str_bytes"] - by_size[1]["str_bytes"]) / 1024
    utf8_delta_kib = (by_size[32]["utf8_bytes"] - by_size[1]["utf8_bytes"]) / 1024
    limit_kib = multiplier * str_delta_kib
    return {
        "complete": set(by_size) == set(SIZES_MIB),
        "rss_32_minus_1_kib": rss_delta_kib,
        "str_32_minus_1_kib": round(str_delta_kib, 3),
        "utf8_32_minus_1_kib": round(utf8_delta_kib, 3),
        # Judged: RSS growth per byte of the in-memory string.
        "str_ratio": round(rss_delta_kib / str_delta_kib, 3),
        # Recorded only, on the pre-2026-11-07 basis, for later comparison.
        "utf8_ratio": round(rss_delta_kib / utf8_delta_kib, 3),
        "rss_limit_kib": round(limit_kib, 3),
        "rss_pass": rss_delta_kib <= limit_kib,
    }


def _run_parent(output: pathlib.Path | None, approved_multiplier: float | None) -> int:
    samples: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for kind in TEXT_KINDS:
        for size_mib in SIZES_MIB:
            sample, failure = _run_worker(size_mib, kind)
            if sample is not None:
                samples.append(sample)
            if failure is not None:
                failures.append(failure)
    multiplier = approved_multiplier or CANDIDATE_RSS_MULTIPLIER
    by_kind: dict[str, dict[str, Any]] = {}
    for kind in TEXT_KINDS:
        by_size = {s["size_mib"]: s for s in samples if s.get("kind") == kind}
        by_kind[kind] = _kind_summary(by_size, multiplier)
    rss_candidate_pass = all(summary["rss_pass"] for summary in by_kind.values())
    correctness_pass = (
        not failures
        and len(samples) == len(SIZES_MIB) * len(TEXT_KINDS)
        and all(sample["correctness_pass"] for sample in samples)
    )
    owner_approved = approved_multiplier is not None
    report = {
        "schema_version": 2,
        "benchmark": "giant_entry_memory",
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "ru_maxrss_unit": "KiB (Linux)",
        },
        "threshold_definition": THRESHOLD_DEFINITION,
        "previous_threshold_definition": PREVIOUS_THRESHOLD_DEFINITION,
        "known_limitations": [
            "Mixed-width text is held at 2-4x its UTF-8 size in CPython "
            "(one BMP character such as IPA doubles it, one astral character "
            "quadruples it); the full entry text is a str for Copy and Open full.",
            "Pure BMP CJK is the worst case under the str basis: its str is about "
            "0.67x its UTF-8 size, so any UTF-8 copy alive at the peak weighs about "
            "1.5x against the str denominator. The benchmark starts from "
            "render_entries; the sdcv path also holds the JSON stdout bytes and "
            "their decoded copy while parsing, which this benchmark does not measure. "
            "Freeing the raw bytes right after decoding would lower that peak.",
        ],
        "text_kinds": list(TEXT_KINDS),
        "raw_samples": samples,
        "failures": failures,
        "by_kind": by_kind,
        "summary": {
            "correctness_pass": correctness_pass,
            "rss_multiplier": multiplier,
            "rss_basis": "cpython_str_bytes",
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
    parser.add_argument("--worker-kind", choices=TEXT_KINDS, default="ascii")
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument(
        "--approved-rss-multiplier",
        type=float,
        help="enforce an owner-approved multiplier; omitted means proposal-only",
    )
    args = parser.parse_args()
    if args.worker_size_mib is not None:
        sys.stdout.write(json.dumps(_worker(args.worker_size_mib, args.worker_kind)) + "\n")
        return 0
    if args.approved_rss_multiplier is not None and args.approved_rss_multiplier <= 0:
        parser.error("--approved-rss-multiplier must be positive")
    return _run_parent(args.output, args.approved_rss_multiplier)


if __name__ == "__main__":
    raise SystemExit(main())
