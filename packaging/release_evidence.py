#!/usr/bin/env python3
"""Bind one release evidence set to current source bytes and one session."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import stat
import struct
import subprocess
import sys
import uuid
from typing import Iterable, Mapping, Sequence


AUDIT_HANDOFF_NAME = "AUDIT-RELEASE-BLOCKERS-使用者看過並表示認可.md"
EVIDENCE_PREFIX = pathlib.PurePosixPath("tests/evidence")
CONTEXT_SCHEMA_VERSION = 1
BINDING_SCHEMA_VERSION = 1
SOURCE_MANIFEST_SCHEMA_VERSION = 2
SOURCE_MANIFEST_ALGORITHM = "sha256-regular-path-mode-size-and-bytes-v2"
_CACHE_PARTS = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache"}
)
_ARCHIVE_SUFFIXES = (
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.zst",
    ".tzst",
    ".zip",
    ".zst",
)
_GENERATED_SUFFIXES = (".log", ".pyc", ".pyo", ".tmp", ".temp", ".swp", ".swo")
_SECRET_HEADER = struct.Struct(">I")


class ReleaseEvidenceError(RuntimeError):
    pass


def _read_exact_fd(fd: int, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = os.read(fd, size - len(chunks))
        if not chunk:
            raise ReleaseEvidenceError("unexpected EOF on protected sentinel FD")
        chunks.extend(chunk)
    return bytes(chunks)


def read_secret_fd(fd: int) -> bytes:
    try:
        size = _SECRET_HEADER.unpack(_read_exact_fd(fd, _SECRET_HEADER.size))[0]
        if size < 32 or size > 4096:
            raise ReleaseEvidenceError("invalid protected sentinel length")
        value = _read_exact_fd(fd, size)
        if os.read(fd, 1):
            raise ReleaseEvidenceError("protected sentinel FD has trailing data")
        return value
    finally:
        os.close(fd)


def write_secret_fd(fd: int, value: bytes | bytearray | memoryview) -> None:
    if len(value) < 32 or len(value) > 4096:
        raise ReleaseEvidenceError("invalid protected sentinel length")
    try:
        for part in (_SECRET_HEADER.pack(len(value)), value):
            view = memoryview(part)
            while view:
                written = os.write(fd, view)
                view = view[written:]
    finally:
        os.close(fd)


def _safe_relative(value: str | pathlib.PurePosixPath) -> pathlib.PurePosixPath:
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() in {"", "."}:
        raise ReleaseEvidenceError(f"unsafe relative path: {value}")
    return path


def is_evidence_path(path: pathlib.PurePosixPath) -> bool:
    return path == EVIDENCE_PREFIX or EVIDENCE_PREFIX in path.parents


def is_excluded_source_candidate(path: pathlib.PurePosixPath) -> bool:
    path = _safe_relative(path)
    name = path.name
    lowered = name.casefold()
    if ".git" in path.parts or is_evidence_path(path):
        return True
    if name == AUDIT_HANDOFF_NAME or any(part in _CACHE_PARTS for part in path.parts):
        return True
    if lowered.endswith(_ARCHIVE_SUFFIXES) or lowered.endswith(_GENERATED_SUFFIXES):
        return True
    if (
        name.endswith("~")
        or ".tmp-" in lowered
        or name.startswith(".dank-lookup-review-")
    ):
        return True
    return False


def git_candidate_paths(root: pathlib.Path) -> list[pathlib.PurePosixPath]:
    run = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if run.returncode != 0:
        raise ReleaseEvidenceError("could not enumerate Git source candidates")
    candidates: set[pathlib.PurePosixPath] = set()
    for raw in run.stdout.split(b"\0"):
        if not raw:
            continue
        try:
            relative = _safe_relative(raw.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise ReleaseEvidenceError("source candidate path is not UTF-8") from exc
        candidates.add(relative)
    return sorted(candidates, key=lambda item: item.as_posix())


def source_candidate_paths(
    root: pathlib.Path,
    candidates: Iterable[pathlib.PurePosixPath] | None = None,
) -> list[pathlib.PurePosixPath]:
    selected: list[pathlib.PurePosixPath] = []
    for relative in candidates if candidates is not None else git_candidate_paths(root):
        relative = _safe_relative(relative)
        if is_excluded_source_candidate(relative):
            continue
        path = root / relative.as_posix()
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        _validated_source_mode(relative, info)
        selected.append(relative)
    return sorted(set(selected), key=lambda item: item.as_posix())


def _validated_source_mode(
    relative: pathlib.PurePosixPath,
    info: os.stat_result,
) -> int:
    if not stat.S_ISREG(info.st_mode):
        raise ReleaseEvidenceError(
            f"source candidate is not a regular file: {relative.as_posix()}"
        )
    mode = stat.S_IMODE(info.st_mode)
    if mode & (stat.S_ISUID | stat.S_ISGID):
        raise ReleaseEvidenceError(
            f"source candidate has setuid or setgid bits: {relative.as_posix()}"
        )
    return mode


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_source_manifest(
    root: pathlib.Path,
    candidates: Iterable[pathlib.PurePosixPath] | None = None,
) -> dict[str, object]:
    paths = source_candidate_paths(root, candidates)
    overall = hashlib.sha256()
    overall.update(b"dank-lookup-source-manifest\0")
    overall.update(SOURCE_MANIFEST_ALGORITHM.encode("ascii"))
    overall.update(b"\0")
    entries: list[dict[str, object]] = []
    for relative in paths:
        encoded_path = relative.as_posix().encode("utf-8")
        source = root / relative.as_posix()
        before = source.lstat()
        _validated_source_mode(relative, before)
        try:
            descriptor = os.open(
                source,
                os.O_RDONLY | os.O_NOFOLLOW,
            )
        except OSError as exc:
            raise ReleaseEvidenceError(
                f"could not open regular source candidate: {relative.as_posix()}"
            ) from exc
        try:
            opened = os.fstat(descriptor)
            mode = _validated_source_mode(relative, opened)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ReleaseEvidenceError(
                    f"source candidate changed while opening: {relative.as_posix()}"
                )
            size = opened.st_size
            file_digest = hashlib.sha256()
            overall.update(b"regular\0")
            overall.update(len(encoded_path).to_bytes(8, "big"))
            overall.update(encoded_path)
            overall.update(mode.to_bytes(4, "big"))
            overall.update(size.to_bytes(16, "big"))
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                while chunk := handle.read(1024 * 1024):
                    file_digest.update(chunk)
                    overall.update(chunk)
            overall.update(b"\0end\0")
        finally:
            os.close(descriptor)
        entries.append(
            {
                "path": relative.as_posix(),
                "type": "regular",
                "mode": mode,
                "size": size,
                "sha256": file_digest.hexdigest(),
            }
        )
    return {
        "schema_version": SOURCE_MANIFEST_SCHEMA_VERSION,
        "algorithm": SOURCE_MANIFEST_ALGORITHM,
        "digest": overall.hexdigest(),
        "files": entries,
    }


def _atomic_write_json(path: pathlib.Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _load_json(path: pathlib.Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseEvidenceError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ReleaseEvidenceError(f"invalid JSON object: {path}")
    return value


def _validate_session(value: object) -> str:
    try:
        session_id = str(uuid.UUID(str(value)))
    except (ValueError, AttributeError) as exc:
        raise ReleaseEvidenceError("invalid release session ID") from exc
    return session_id


def _binding_identity(value: Mapping[str, object]) -> tuple[str, str]:
    session_id = _validate_session(value.get("release_session_id"))
    source_manifest = value.get("source_manifest")
    _validate_source_manifest(source_manifest)
    assert isinstance(source_manifest, Mapping)
    source_digest = source_manifest.get("digest")
    if (
        not isinstance(source_digest, str)
        or len(source_digest) != 64
        or any(character not in "0123456789abcdef" for character in source_digest)
    ):
        raise ReleaseEvidenceError("release source digest is invalid")
    return session_id, source_digest


def _validate_source_manifest(value: object) -> None:
    if (
        not isinstance(value, Mapping)
        or value.get("schema_version") != SOURCE_MANIFEST_SCHEMA_VERSION
        or value.get("algorithm") != SOURCE_MANIFEST_ALGORITHM
        or not isinstance(value.get("files"), list)
    ):
        raise ReleaseEvidenceError("unsupported release source manifest")


def create_context(root: pathlib.Path, context_path: pathlib.Path) -> dict[str, object]:
    if context_path.exists():
        raise ReleaseEvidenceError("release context already exists")
    context: dict[str, object] = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "kind": "dank_lookup_release_context",
        "release_session_id": str(uuid.uuid4()),
        "created_utc": dt.datetime.now(dt.UTC).isoformat(),
        "source_manifest": canonical_source_manifest(root),
        "evidence": {},
    }
    _atomic_write_json(context_path, context)
    return context


def load_context(context_path: pathlib.Path) -> dict[str, object]:
    context = _load_json(context_path)
    if (
        context.get("schema_version") != CONTEXT_SCHEMA_VERSION
        or context.get("kind") != "dank_lookup_release_context"
        or not isinstance(context.get("source_manifest"), dict)
        or not isinstance(context.get("evidence"), dict)
    ):
        raise ReleaseEvidenceError("unsupported release context")
    _binding_identity(context)
    return context


def verify_source_unchanged(root: pathlib.Path, context: Mapping[str, object]) -> None:
    expected = context.get("source_manifest")
    current = canonical_source_manifest(root)
    if current != expected:
        raise ReleaseEvidenceError("current source candidates do not match release context")


def _relative_evidence_path(root: pathlib.Path, path: pathlib.Path) -> pathlib.PurePosixPath:
    try:
        relative = _safe_relative(path.resolve().relative_to(root.resolve()).as_posix())
    except ValueError as exc:
        raise ReleaseEvidenceError("evidence path escapes repository") from exc
    if not is_evidence_path(relative):
        raise ReleaseEvidenceError("release evidence must be under tests/evidence")
    info = (root / relative.as_posix()).lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ReleaseEvidenceError("release evidence is not a regular file")
    return relative


def record_evidence(
    root: pathlib.Path,
    context_path: pathlib.Path,
    evidence_paths: Sequence[pathlib.Path],
) -> dict[str, object]:
    context = load_context(context_path)
    verify_source_unchanged(root, context)
    release_session_id, source_digest = _binding_identity(context)
    evidence = dict(context["evidence"])
    for path in evidence_paths:
        relative = _relative_evidence_path(root, path)
        source = root / relative.as_posix()
        evidence[relative.as_posix()] = {
            "sha256": sha256_file(source),
            "size": source.stat().st_size,
            "release_session_id": release_session_id,
            "source_digest": source_digest,
        }
    context["evidence"] = dict(sorted(evidence.items()))
    _atomic_write_json(context_path, context)
    return context


def load_requirements(path: pathlib.Path) -> dict[str, list[str]]:
    value = _load_json(path)
    groups = value.get("required_groups")
    if value.get("schema_version") != 1 or not isinstance(groups, dict):
        raise ReleaseEvidenceError("unsupported evidence requirements")
    normalized: dict[str, list[str]] = {}
    for name, patterns in groups.items():
        if not isinstance(name, str) or not isinstance(patterns, list):
            raise ReleaseEvidenceError("invalid evidence requirement group")
        if not patterns or not all(isinstance(item, str) and item for item in patterns):
            raise ReleaseEvidenceError("invalid evidence requirement pattern")
        for pattern in patterns:
            relative = pathlib.PurePosixPath(pattern)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or EVIDENCE_PREFIX not in relative.parents
            ):
                raise ReleaseEvidenceError(
                    "evidence requirement escapes tests/evidence"
                )
        normalized[name] = list(patterns)
    return normalized


def _group_paths(
    root: pathlib.Path,
    groups: Mapping[str, Sequence[str]],
    recorded: set[str],
) -> dict[str, list[str]]:
    answer: dict[str, list[str]] = {}
    for name, patterns in groups.items():
        matches = {
            path.relative_to(root).as_posix()
            for pattern in patterns
            for path in root.glob(pattern)
            if path.is_file()
            and not path.is_symlink()
            and path.relative_to(root).as_posix() in recorded
        }
        answer[name] = sorted(matches)
    return answer


def verify_recorded_evidence(
    root: pathlib.Path,
    context: Mapping[str, object],
) -> None:
    evidence = context.get("evidence")
    if not isinstance(evidence, dict):
        raise ReleaseEvidenceError("release context evidence registry is invalid")
    release_session_id, source_digest = _binding_identity(context)
    for relative, metadata in evidence.items():
        if not isinstance(relative, str) or not isinstance(metadata, dict):
            raise ReleaseEvidenceError("release context evidence entry is invalid")
        path = root / _safe_relative(relative).as_posix()
        if not path.is_file() or path.is_symlink():
            raise ReleaseEvidenceError(f"recorded evidence is missing: {relative}")
        if (
            metadata.get("release_session_id") != release_session_id
            or metadata.get("source_digest") != source_digest
            or metadata.get("sha256") != sha256_file(path)
            or metadata.get("size") != path.stat().st_size
        ):
            raise ReleaseEvidenceError(f"recorded evidence changed: {relative}")


def missing_recorded_groups(
    root: pathlib.Path,
    context: Mapping[str, object],
    groups: Mapping[str, Sequence[str]],
    *,
    exempt: Iterable[str] = (),
) -> list[str]:
    evidence = context.get("evidence")
    if not isinstance(evidence, dict):
        raise ReleaseEvidenceError("release context evidence registry is invalid")
    selected = _group_paths(root, groups, set(evidence))
    exempt_names = set(exempt)
    return sorted(name for name, paths in selected.items() if not paths and name not in exempt_names)


def finalize_binding(
    root: pathlib.Path,
    context_path: pathlib.Path,
    requirements_path: pathlib.Path,
    output: pathlib.Path,
) -> dict[str, object]:
    context = load_context(context_path)
    verify_source_unchanged(root, context)
    verify_recorded_evidence(root, context)
    groups = load_requirements(requirements_path)
    missing = missing_recorded_groups(root, context, groups, exempt={"release_binding"})
    if missing:
        raise ReleaseEvidenceError("missing bound evidence groups: " + ", ".join(missing))
    release_session_id = _validate_session(context["release_session_id"])
    relative_output = _relative_output_path(root, output)
    if output.name != f"{release_session_id}.json":
        raise ReleaseEvidenceError("release binding filename must match its session ID")
    if output.exists():
        raise ReleaseEvidenceError("release binding already exists")
    binding_patterns = groups.get("release_binding", [])
    if not any(
        pathlib.PurePosixPath(relative_output).match(pattern)
        for pattern in binding_patterns
    ):
        raise ReleaseEvidenceError("release binding output does not match requirements")
    evidence = dict(context["evidence"])
    if any(
        pathlib.PurePosixPath(relative).match(pattern)
        for relative in evidence
        for pattern in binding_patterns
    ):
        raise ReleaseEvidenceError("release binding cannot be pre-recorded as evidence")
    selected_groups = _group_paths(root, groups, set(evidence))
    selected_groups["release_binding"] = [relative_output]
    binding: dict[str, object] = {
        "schema_version": BINDING_SCHEMA_VERSION,
        "kind": "dank_lookup_release_evidence_binding",
        "release_session_id": release_session_id,
        "created_utc": dt.datetime.now(dt.UTC).isoformat(),
        "source_manifest": context["source_manifest"],
        "evidence": evidence,
        "groups": selected_groups,
    }
    _atomic_write_json(output, binding)
    return binding


def _relative_output_path(root: pathlib.Path, output: pathlib.Path) -> str:
    try:
        relative = _safe_relative(output.resolve().relative_to(root.resolve()).as_posix())
    except ValueError as exc:
        raise ReleaseEvidenceError("release binding output escapes repository") from exc
    if not is_evidence_path(relative):
        raise ReleaseEvidenceError("release binding must be under tests/evidence")
    return relative.as_posix()


def verify_binding(
    root: pathlib.Path,
    requirements_path: pathlib.Path,
    binding_path: pathlib.Path,
) -> tuple[dict[str, object], dict[str, list[pathlib.Path]]]:
    binding = _load_json(binding_path)
    if (
        binding.get("schema_version") != BINDING_SCHEMA_VERSION
        or binding.get("kind") != "dank_lookup_release_evidence_binding"
        or not isinstance(binding.get("source_manifest"), dict)
        or not isinstance(binding.get("evidence"), dict)
        or not isinstance(binding.get("groups"), dict)
    ):
        raise ReleaseEvidenceError("unsupported release evidence binding")
    release_session_id = _validate_session(binding.get("release_session_id"))
    if binding_path.name != f"{release_session_id}.json":
        raise ReleaseEvidenceError("release binding filename does not match its session ID")
    current = canonical_source_manifest(root)
    if current != binding["source_manifest"]:
        raise ReleaseEvidenceError("release binding source digest does not match current source")
    verify_recorded_evidence(root, binding)
    groups = load_requirements(requirements_path)
    evidence = binding["evidence"]
    assert isinstance(evidence, dict)
    expected_groups = _group_paths(root, groups, set(evidence))
    relative_binding = _relative_output_path(root, binding_path)
    expected_groups["release_binding"] = [relative_binding]
    if binding["groups"] != expected_groups:
        raise ReleaseEvidenceError("release binding group mapping is invalid")
    missing = sorted(name for name in groups if not expected_groups.get(name))
    if missing:
        raise ReleaseEvidenceError("release binding lacks mandatory groups: " + ", ".join(missing))
    selected = {
        name: [root / relative for relative in paths]
        for name, paths in expected_groups.items()
    }
    return binding, selected


def verify_staged_source(
    staging: pathlib.Path,
    source_manifest: Mapping[str, object],
) -> None:
    _validate_source_manifest(source_manifest)
    files = source_manifest.get("files")
    if not isinstance(files, list) or not all(isinstance(item, dict) for item in files):
        raise ReleaseEvidenceError("source manifest file list is invalid")
    candidates = [
        _safe_relative(str(item.get("path", "")))
        for item in files
    ]
    current = canonical_source_manifest(staging, candidates)
    if current != source_manifest:
        raise ReleaseEvidenceError("staging source digest does not match release evidence")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parent.parent)
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start")
    start.add_argument("--context", required=True, type=pathlib.Path)

    record = subparsers.add_parser("record")
    record.add_argument("--context", required=True, type=pathlib.Path)
    record.add_argument("--path", action="append", required=True, type=pathlib.Path)

    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("--context", required=True, type=pathlib.Path)
    finalize.add_argument("--requirements", required=True, type=pathlib.Path)
    finalize.add_argument("--output", required=True, type=pathlib.Path)

    args = parser.parse_args(argv)
    try:
        if args.command == "start":
            value = create_context(args.root, args.context)
            print(value["release_session_id"])
            print(value["source_manifest"]["digest"])
        elif args.command == "record":
            record_evidence(args.root, args.context, args.path)
        else:
            finalize_binding(args.root, args.context, args.requirements, args.output)
    except ReleaseEvidenceError as exc:
        print(f"release-evidence: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
