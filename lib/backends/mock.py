#!/usr/bin/env python3
"""Deterministic no-network mock adapter for UI/error-state tests."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import tempfile
import time
import uuid


MAX_STDIN_QUERY_BYTES = 64 * 1024


def atomic_output(path: pathlib.Path, payload: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise RuntimeError("refusing symlink output")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = pathlib.Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--stdout", action="store_true")
    parser.add_argument("--request-id")
    parser.add_argument("--sequence", type=int)
    parser.add_argument("--stdin", action="store_true")
    args = parser.parse_args(argv)
    if not args.stdin:
        raise SystemExit("--stdin is required; positional queries are not supported")
    payload = sys.stdin.buffer.read(MAX_STDIN_QUERY_BYTES + 1)
    if len(payload) > MAX_STDIN_QUERY_BYTES:
        raise SystemExit("stdin query exceeds 65536-byte limit")
    try:
        query = payload.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise SystemExit("stdin query is not valid UTF-8") from exc
    started = time.monotonic_ns()
    result = {
        "schema_version": 2,
        "backend": "mock",
        "query": query,
        "request_id": args.request_id or str(uuid.uuid4()),
        "sequence": args.sequence if args.sequence is not None else started,
        "title": query,
        "subtitle": "Mock dictionary",
        "body": "Deterministic offline mock definition.",
        "truncated": False,
        "candidate_modes": ["mock"],
        "installed_modes": ["mock"],
        "unavailable_modes": [],
        "available_modes": ["mock"],
        "default_mode": "mock",
        "results_by_mode": {
            "mock": {
                "label": "Mock",
                "subtitle": "Mock dictionary",
                "body": "Deterministic offline mock definition.",
                "full_body": "Deterministic offline mock definition.",
                "truncated": False,
                "entries": [],
                "entry_count": 0,
                "preview_entry_count": 0,
            }
        },
        "related_by_mode": {},
        "mode_preferences": {"mock": {"show_related": False}},
        "segmented": False,
        "tokens": [],
        "token_items": [],
        "results_by_token": {},
        "error": None,
        "runtime": {"retained": False, "accepted_as_active": False},
        "timing": {
            "started_monotonic_ns": started,
            "backend_ms": 0.0,
            "total_ms": round((time.monotonic_ns() - started) / 1_000_000, 3),
        },
    }
    payload = json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    configured = None if args.stdout else os.environ.get("DANK_LOOKUP_OUTPUT")
    if configured:
        output = pathlib.Path(configured)
        atomic_output(output, payload)
    else:
        sys.stdout.buffer.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
