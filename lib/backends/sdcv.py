#!/usr/bin/env python3
"""Command-line adapter around the standalone Dank Lookup core."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import tempfile


LIB_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from dank_lookup_core import lookup  # noqa: E402
from dank_lookup_runtime import write_result  # noqa: E402


MAX_STDIN_QUERY_BYTES = 64 * 1024


def _atomic_output(path: pathlib.Path, payload: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise RuntimeError(f"refusing symlink output: {path}")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = pathlib.Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline StarDict lookup backend")
    parser.add_argument("--stdout", action="store_true", help="write JSON to stdout instead of a legacy output file")
    parser.add_argument("--request-id")
    parser.add_argument("--sequence", type=int)
    parser.add_argument("--config")
    parser.add_argument(
        "--retain-runtime",
        action="store_true",
        help="opt in to request-scoped retention (off by default; intended for compatibility/tests)",
    )
    parser.add_argument("--output", help="legacy JSON output path")
    parser.add_argument("--stdin", action="store_true", help="read the lookup query from bounded stdin")
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = build_parser().parse_args(argv)
    if not args.stdin:
        raise SystemExit("--stdin is required; positional queries are not supported")
    query_bytes = sys.stdin.buffer.read(MAX_STDIN_QUERY_BYTES + 1)
    if len(query_bytes) > MAX_STDIN_QUERY_BYTES:
        raise SystemExit("stdin query exceeds 65536-byte limit")
    try:
        query = query_bytes.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise SystemExit("stdin query is not valid UTF-8") from exc
    result, vault = lookup(
        query,
        config_path=args.config,
        request_id=args.request_id,
        sequence=args.sequence,
        retain_entry_vault=args.retain_runtime,
        retain_full_blobs=args.retain_runtime,
    )
    if args.retain_runtime:
        accepted = write_result(result, vault)
        result["runtime"] = {
            "retained": True,
            "accepted_as_active": accepted,
            "request_id": result["request_id"],
        }
    else:
        result["runtime"] = {"retained": False, "accepted_as_active": False}
    payload = json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    configured_output = None if args.stdout else (args.output or os.environ.get("DANK_LOOKUP_OUTPUT"))
    if configured_output:
        output = pathlib.Path(configured_output)
        _atomic_output(output, payload)
    else:
        # Stdout is the privacy-preserving default. File output is an explicit
        # compatibility choice via --output or DANK_LOOKUP_OUTPUT.
        sys.stdout.buffer.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
