#!/usr/bin/env python3
"""Validate and compose Dank Lookup's installed managed-files manifest."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import tempfile
from typing import Any


SCHEMA_VERSION = 1
ROOT_KEYS = (
    "home_bin",
    "app_lib",
    "quickshell",
    "systemd_user",
    "matugen",
    "dms",
)


class ManifestError(ValueError):
    pass


def validate_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ManifestError("managed path must be a non-empty string")
    if "\n" in value or "\r" in value or "\t" in value or "\\" in value:
        raise ManifestError("managed path contains a forbidden character")
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ManifestError("managed path is not a normalized relative path")
    if path.as_posix() != value:
        raise ManifestError("managed path is not normalized")
    return value


def validate_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise ManifestError("unsupported managed-files schema")
    roots = value.get("roots")
    if not isinstance(roots, dict) or set(roots) - set(ROOT_KEYS):
        raise ManifestError("invalid managed root key")
    normalized: dict[str, list[str]] = {}
    for root in ROOT_KEYS:
        entries = roots.get(root, [])
        if not isinstance(entries, list):
            raise ManifestError("managed root entries must be a list")
        paths = [validate_relative_path(item) for item in entries]
        if len(paths) != len(set(paths)):
            raise ManifestError("duplicate managed path")
        normalized[root] = sorted(paths)
    return {"schema_version": SCHEMA_VERSION, "roots": normalized}


def load_manifest(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"could not read managed manifest: {path}") from exc
    return validate_manifest(value)


def read_records(path: pathlib.Path) -> dict[str, list[str]]:
    roots = {root: [] for root in ROOT_KEYS}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ManifestError("could not read managed records") from exc
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 2 or fields[0] not in roots:
            raise ManifestError("invalid managed record")
        roots[fields[0]].append(validate_relative_path(fields[1]))
    for root, entries in roots.items():
        if len(entries) != len(set(entries)):
            raise ManifestError(f"duplicate managed record in {root}")
        roots[root] = sorted(entries)
    return roots


def atomic_write_manifest(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        pathlib.Path(temporary).unlink(missing_ok=True)
        raise


def build_manifest(
    records_path: pathlib.Path,
    old_path: pathlib.Path | None,
    preserve_roots: set[str],
) -> dict[str, Any]:
    roots = read_records(records_path)
    if old_path is not None:
        old = load_manifest(old_path)
        for root in preserve_roots:
            if root not in ROOT_KEYS:
                raise ManifestError("invalid preserved root")
            roots[root] = sorted(set(roots[root]) | set(old["roots"][root]))
    return validate_manifest({"schema_version": SCHEMA_VERSION, "roots": roots})


def emit_paths(manifest: dict[str, Any]) -> None:
    for root in ROOT_KEYS:
        for relative in manifest["roots"][root]:
            print(f"{root}\t{relative}")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("manifest", type=pathlib.Path)

    paths_parser = subparsers.add_parser("paths")
    paths_parser.add_argument("manifest", type=pathlib.Path)

    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("--records", required=True, type=pathlib.Path)
    build_parser.add_argument("--output", required=True, type=pathlib.Path)
    build_parser.add_argument("--old", type=pathlib.Path)
    build_parser.add_argument("--preserve-root", action="append", default=[])

    stale_parser = subparsers.add_parser("stale")
    stale_parser.add_argument("--old", required=True, type=pathlib.Path)
    stale_parser.add_argument("--new", required=True, type=pathlib.Path)

    args = parser.parse_args()
    try:
        if args.command == "validate":
            load_manifest(args.manifest)
        elif args.command == "paths":
            emit_paths(load_manifest(args.manifest))
        elif args.command == "build":
            value = build_manifest(
                args.records,
                args.old,
                set(args.preserve_root),
            )
            atomic_write_manifest(args.output, value)
        elif args.command == "stale":
            old = load_manifest(args.old)
            new = load_manifest(args.new)
            stale = {
                root: sorted(set(old["roots"][root]) - set(new["roots"][root]))
                for root in ROOT_KEYS
            }
            emit_paths({"schema_version": SCHEMA_VERSION, "roots": stale})
    except ManifestError as exc:
        print(f"managed-files: {exc}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
