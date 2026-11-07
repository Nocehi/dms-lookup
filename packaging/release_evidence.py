#!/usr/bin/env python3
"""Bind release evidence to one Git commit.

  start   create a release context for a commit (default HEAD)
  run     run the commands that the commit's requirements file fixes for an
          automated evidence group, in a temporary worktree of that commit,
          and record the log and declared outputs
  record  record an owner-run (manual) evidence file
  adopt   carry manual evidence over from an older context

Automated evidence is never carried over: it is re-run for every commit.
Manual evidence names the commit it measured (``tested_source``). It counts
for the release commit when the two differ in no path the group depends on:
the group's ``depends_on`` globs, or, when a group declares none, any path
that is not documentation.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import uuid
from typing import Iterable, Mapping

CONTEXT_SCHEMA_VERSION = 2
REQUIREMENTS_SCHEMA_VERSION = 2
REQUIREMENTS_PATH = "packaging/review-evidence-requirements.json"
SESSION_ROOT = pathlib.PurePosixPath("tests/evidence/release")
RUN_PASS = "RUN: PASS"
RUN_FAIL = "RUN: FAIL"


class ReleaseEvidenceError(RuntimeError):
    pass


def git(root: pathlib.Path, *args: str) -> str:
    run = subprocess.run(
        ["git", *args],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if run.returncode != 0:
        raise ReleaseEvidenceError(f"git {' '.join(args)} failed: {run.stderr.strip()}")
    return run.stdout


def resolve_commit(root: pathlib.Path, rev: str) -> tuple[str, str]:
    commit = git(root, "rev-parse", "--verify", f"{rev}^{{commit}}").strip()
    tree = git(root, "rev-parse", f"{commit}^{{tree}}").strip()
    return commit, tree


def is_documentation_path(path: str) -> bool:
    return path.endswith(".md") or path.startswith("docs/")


def changed_paths(root: pathlib.Path, old: str, new: str) -> list[str]:
    return [line for line in git(root, "diff", "--name-only", old, new).splitlines() if line]


def invalidating_changes(
    root: pathlib.Path, group: Mapping[str, object], old: str, new: str
) -> list[str]:
    """Paths changed between two commits that a manual group depends on."""

    if old == new:
        return []
    depends_on = group.get("depends_on") or []
    paths = changed_paths(root, old, new)
    if depends_on:
        return [p for p in paths if any(fnmatch.fnmatchcase(p, g) for g in depends_on)]
    return [p for p in paths if not is_documentation_path(p)]


def tested_commit(path: pathlib.Path) -> str:
    """The commit a manual evidence file says it measured."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseEvidenceError(f"manual evidence is not JSON: {path.name}") from exc
    source = value.get("tested_source") if isinstance(value, dict) else None
    if not isinstance(source, dict) or not isinstance(source.get("commit"), str):
        raise ReleaseEvidenceError(
            f"manual evidence does not name the commit it measured: {path.name} "
            "(re-take it with the current tools)"
        )
    if source.get("uncommitted_changes") is not False:
        raise ReleaseEvidenceError(f"manual evidence measured uncommitted code: {path.name}")
    return source["commit"]


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _write_json(path: pathlib.Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _load_json(path: pathlib.Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseEvidenceError(f"could not read JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ReleaseEvidenceError(f"JSON root is not an object: {path}")
    return value


def _validate_requirements(value: dict) -> dict:
    if value.get("schema_version") != REQUIREMENTS_SCHEMA_VERSION:
        raise ReleaseEvidenceError("unsupported evidence requirements schema")
    automated = value.get("automated")
    manual = value.get("manual")
    decisions = value.get("decisions", {})
    if not isinstance(automated, dict) or not isinstance(manual, dict) or not isinstance(decisions, dict):
        raise ReleaseEvidenceError("evidence requirements need automated, manual and decisions maps")
    if set(automated) & set(manual):
        raise ReleaseEvidenceError("an evidence group cannot be both automated and manual")
    for name, group in automated.items():
        commands = group.get("commands") if isinstance(group, dict) else None
        outputs = group.get("outputs", []) if isinstance(group, dict) else None
        if (
            not isinstance(commands, list)
            or not commands
            or not all(
                isinstance(argv, list) and argv and all(isinstance(arg, str) for arg in argv)
                for argv in commands
            )
            or not isinstance(outputs, list)
            or not all(isinstance(item, str) and "/" not in item for item in outputs)
        ):
            raise ReleaseEvidenceError(f"automated group {name} is malformed")
    for name, group in manual.items():
        patterns = group.get("patterns") if isinstance(group, dict) else None
        depends_on = group.get("depends_on", []) if isinstance(group, dict) else None
        if (
            not isinstance(patterns, list)
            or not patterns
            or not all(isinstance(p, str) for p in patterns)
            or not isinstance(depends_on, list)
            or not all(isinstance(p, str) for p in depends_on)
        ):
            raise ReleaseEvidenceError(f"manual group {name} is malformed")
    return value


def load_requirements(root: pathlib.Path, commit: str) -> dict:
    """Read the requirements from the release commit, never from the working copy."""

    text = git(root, "show", f"{commit}:{REQUIREMENTS_PATH}")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReleaseEvidenceError("evidence requirements are not valid JSON") from exc
    if not isinstance(value, dict):
        raise ReleaseEvidenceError("evidence requirements root is not an object")
    return _validate_requirements(value)


def create_context(root: pathlib.Path, context_path: pathlib.Path, rev: str = "HEAD") -> dict:
    if context_path.exists():
        raise ReleaseEvidenceError("release context already exists")
    commit, tree = resolve_commit(root, rev)
    load_requirements(root, commit)
    context = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "kind": "dank_lookup_release_context",
        "release_session_id": str(uuid.uuid4()),
        "created_utc": _now(),
        "commit": commit,
        "tree": tree,
        "evidence": {},
    }
    _write_json(context_path, context)
    return context


def load_context(context_path: pathlib.Path) -> dict:
    context = _load_json(context_path)
    if context.get("schema_version") == 1:
        raise ReleaseEvidenceError(
            "schema-1 context (0.8.0 and earlier) is not bound to a commit; its evidence "
            "cannot be adopted, so take owner-run evidence again and record it"
        )
    if (
        context.get("schema_version") != CONTEXT_SCHEMA_VERSION
        or context.get("kind") != "dank_lookup_release_context"
        or not isinstance(context.get("commit"), str)
        or not isinstance(context.get("tree"), str)
        or not isinstance(context.get("evidence"), dict)
    ):
        raise ReleaseEvidenceError("unsupported release context")
    uuid.UUID(str(context.get("release_session_id")))
    return context


def session_dir(context: Mapping[str, object]) -> pathlib.PurePosixPath:
    return SESSION_ROOT / str(context["release_session_id"])


def _relative(root: pathlib.Path, path: pathlib.Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ReleaseEvidenceError(f"evidence must be inside the repository: {path}") from exc


def _expand(argv: list[str], *, out: pathlib.Path, config: pathlib.Path) -> list[str]:
    return [arg.replace("{out}", os.fspath(out)).replace("{config}", os.fspath(config)) for arg in argv]


def run_group(
    root: pathlib.Path,
    context_path: pathlib.Path,
    group: str,
    *,
    config: pathlib.Path,
) -> bool:
    """Run one automated group in a clean worktree of the context commit."""

    context = load_context(context_path)
    commit = str(context["commit"])
    requirements = load_requirements(root, commit)
    definition = requirements["automated"].get(group)
    if definition is None:
        raise ReleaseEvidenceError(f"not an automated evidence group: {group}")
    out = root / session_dir(context).as_posix()
    out.mkdir(parents=True, exist_ok=True)
    log_path = out / f"{group}.log"
    for name in definition.get("outputs", []):
        (out / name).unlink(missing_ok=True)

    lines = [
        f"group: {group}",
        f"commit: {commit}",
        f"release_session_id: {context['release_session_id']}",
        f"started_utc: {_now()}",
        "",
    ]
    if any("{config}" in arg for argv in definition["commands"] for arg in argv):
        digest = sha256_file(config) if config.is_file() else "missing"
        lines.insert(3, f"config: {config} sha256 {digest}")
    passed = True
    with tempfile.TemporaryDirectory(prefix="dank-lookup-release-") as temporary:
        worktree = pathlib.Path(temporary) / "source"
        git(root, "worktree", "add", "--detach", "--quiet", os.fspath(worktree), commit)
        try:
            for argv in definition["commands"]:
                expanded = _expand(argv, out=out, config=config)
                lines.append(f"$ {shlex.join(expanded)}")
                process = subprocess.run(
                    expanded,
                    cwd=worktree,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
                lines.append(process.stdout.decode("utf-8", errors="replace").rstrip("\n"))
                lines.append(f"[exit {process.returncode}]")
                lines.append("")
                if process.returncode != 0:
                    passed = False
                    break
        finally:
            git(root, "worktree", "remove", "--force", os.fspath(worktree))
    missing = [name for name in definition.get("outputs", []) if not (out / name).is_file()]
    if passed and missing:
        lines.append("missing outputs: " + ", ".join(missing))
        passed = False
    lines.append(f"finished_utc: {_now()}")
    lines.append(RUN_PASS if passed else RUN_FAIL)
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    evidence = context["evidence"]
    for relative in [path for path, entry in evidence.items() if entry.get("group") == group]:
        del evidence[relative]
    if passed:
        for path in [log_path, *(out / name for name in definition.get("outputs", []))]:
            evidence[_relative(root, path)] = {
                "group": group,
                "kind": "automated",
                "commit": commit,
                "sha256": sha256_file(path),
            }
    _write_json(context_path, context)
    return passed


def record_manual(
    root: pathlib.Path,
    context_path: pathlib.Path,
    group: str,
    paths: Iterable[pathlib.Path],
) -> None:
    context = load_context(context_path)
    requirements = load_requirements(root, str(context["commit"]))
    definition = requirements["manual"].get(group)
    if definition is None:
        raise ReleaseEvidenceError(f"not a manual evidence group: {group}")
    patterns = definition["patterns"]
    for path in paths:
        relative = _relative(root, path)
        if not path.is_file():
            raise ReleaseEvidenceError(f"manual evidence is not a file: {relative}")
        if not any(pathlib.PurePosixPath(relative).match(pattern) for pattern in patterns):
            raise ReleaseEvidenceError(f"{relative} does not match the {group} patterns")
        measured = tested_commit(path)
        changes = invalidating_changes(root, definition, measured, str(context["commit"]))
        if changes:
            raise ReleaseEvidenceError(
                f"{relative} measured {measured[:12]}, which differs from the release commit in "
                + ", ".join(changes[:10])
            )
        context["evidence"][relative] = {
            "group": group,
            "kind": "manual",
            "commit": measured,
            "sha256": sha256_file(path),
        }
    _write_json(context_path, context)


def adopt_manual(root: pathlib.Path, context_path: pathlib.Path, from_path: pathlib.Path) -> list[str]:
    """Carry manual evidence over when nothing it depends on changed since it was taken."""

    context = load_context(context_path)
    previous = load_context(from_path)
    requirements = load_requirements(root, str(context["commit"]))
    adopted: list[str] = []
    for relative, entry in previous["evidence"].items():
        if entry.get("kind") != "manual" or entry.get("group") not in requirements["manual"]:
            continue
        changes = invalidating_changes(
            root, requirements["manual"][entry["group"]], str(entry["commit"]), str(context["commit"])
        )
        if changes:
            raise ReleaseEvidenceError(
                f"cannot adopt {relative}: changes it depends on since "
                f"{str(entry['commit'])[:12]}: " + ", ".join(changes[:10])
            )
        if sha256_file(root / relative) != entry.get("sha256"):
            raise ReleaseEvidenceError(f"cannot adopt {relative}: file changed since it was recorded")
        context["evidence"][relative] = {**entry, "adopted_into": context["commit"]}
        adopted.append(relative)
    _write_json(context_path, context)
    return adopted


def verify_context(root: pathlib.Path, context: Mapping[str, object]) -> dict[str, list[pathlib.Path]]:
    """Check every recorded checksum and that every required group is present."""

    commit = str(context["commit"])
    requirements = load_requirements(root, commit)
    groups: dict[str, list[pathlib.Path]] = {
        name: [] for name in [*requirements["automated"], *requirements["manual"]]
    }
    evidence = context["evidence"]
    assert isinstance(evidence, dict)
    for relative, entry in sorted(evidence.items()):
        group = entry.get("group")
        if group not in groups:
            raise ReleaseEvidenceError(f"evidence {relative} belongs to no required group")
        path = root / relative
        if not path.is_file() or sha256_file(path) != entry.get("sha256"):
            raise ReleaseEvidenceError(f"evidence changed or missing since it was recorded: {relative}")
        if entry.get("kind") == "automated":
            if entry.get("commit") != commit or group not in requirements["automated"]:
                raise ReleaseEvidenceError(f"automated evidence is not from the release commit: {relative}")
        else:
            if group not in requirements["manual"]:
                raise ReleaseEvidenceError(f"manual evidence in an automated group: {relative}")
            measured = tested_commit(path)
            if measured != entry.get("commit") or invalidating_changes(
                root, requirements["manual"][group], measured, commit
            ):
                raise ReleaseEvidenceError(f"manual evidence measured other code: {relative}")
        groups[group].append(path)
    for name in requirements["automated"]:
        logs = [path for path in groups[name] if path.suffix == ".log"]
        if not logs or not logs[0].read_text(encoding="utf-8", errors="replace").rstrip().endswith(RUN_PASS):
            groups[name] = []
    missing = sorted(name for name, paths in groups.items() if not paths)
    if missing:
        raise ReleaseEvidenceError("missing evidence groups: " + ", ".join(missing))
    return groups


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parent.parent)
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start")
    start.add_argument("--context", required=True, type=pathlib.Path)
    start.add_argument("--commit", default="HEAD")

    run = subparsers.add_parser("run")
    run.add_argument("--context", required=True, type=pathlib.Path)
    which = run.add_mutually_exclusive_group(required=True)
    which.add_argument("--group")
    which.add_argument("--all", action="store_true")
    run.add_argument(
        "--config",
        type=pathlib.Path,
        default=pathlib.Path.home() / ".config/dank-lookup/config.json",
        help="dictionary config passed to benchmarks as {config}",
    )

    record = subparsers.add_parser("record")
    record.add_argument("--context", required=True, type=pathlib.Path)
    record.add_argument("--group", required=True)
    record.add_argument("--path", action="append", required=True, type=pathlib.Path)

    adopt = subparsers.add_parser("adopt")
    adopt.add_argument("--context", required=True, type=pathlib.Path)
    adopt.add_argument("--from", dest="previous", required=True, type=pathlib.Path)

    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "start":
            value = create_context(root, args.context, args.commit)
            print(value["release_session_id"])
            print(value["commit"])
        elif args.command == "run":
            context = load_context(args.context)
            names = (
                list(load_requirements(root, str(context["commit"]))["automated"])
                if args.all
                else [args.group]
            )
            failed = []
            for name in names:
                ok = run_group(root, args.context, name, config=args.config)
                print(f"{name}: {'pass' if ok else 'FAIL'}")
                if not ok:
                    failed.append(name)
            if failed:
                print(
                    "release-evidence: failed groups (see their logs): " + ", ".join(failed),
                    file=sys.stderr,
                )
                return 1
        elif args.command == "record":
            record_manual(root, args.context, args.group, args.path)
        else:
            for relative in adopt_manual(root, args.context, args.previous):
                print(f"adopted {relative}")
    except ReleaseEvidenceError as exc:
        print(f"release-evidence: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
