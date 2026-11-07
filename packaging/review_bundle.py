#!/usr/bin/env python3
"""Build the release review bundle from a completed release context.

The bundle holds ``git archive`` of the release commit, every recorded
evidence file, the captured environment, a binding that names the commit,
tree and checksums, and ``checksums.sha256`` for the non-source files.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import pathlib
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

sys.path.insert(0, os.fspath(pathlib.Path(__file__).resolve().parent))

import release_evidence  # noqa: E402

ARCHIVE_ROOT = "dank-lookup-review"
BINDING_SCHEMA_VERSION = 2
VERSION_PATTERN = re.compile(r"(\d+\.\d+\.\d+)")
ENVIRONMENT_TOOLS = {
    "sdcv": ["sdcv", "--version"],
    "quickshell": ["qs", "--version"],
    "rustc": ["rustc", "--version"],
    "cargo": ["cargo", "--version"],
    "shellcheck": ["shellcheck", "--version"],
    "bash": ["bash", "--version"],
}


class BundleError(RuntimeError):
    pass


def _load_json(path: pathlib.Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleError(f"could not read evidence JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise BundleError(f"evidence JSON root is not an object: {path.name}")
    return value


def _tool_version(argv: list[str]) -> str | None:
    if shutil.which(argv[0]) is None:
        return None
    run = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    text = run.stdout.decode("utf-8", errors="replace").strip()
    return text.splitlines()[0] if text else ""


def capture_environment() -> dict[str, object]:
    return {
        "kind": "dank_lookup_release_environment",
        "captured_utc": dt.datetime.now(dt.UTC).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        **{name: _tool_version(argv) for name, argv in ENVIRONMENT_TOOLS.items()},
    }


def check_decisions(environment: dict[str, object], decisions: dict[str, dict]) -> None:
    """Require a fresh probe when a tool differs from the version a decision was made on."""

    for name, decision in decisions.items():
        tool = str(decision.get("tool", ""))
        recorded = str(decision.get("version", ""))
        found_raw = environment.get(tool)
        match = VERSION_PATTERN.search(found_raw) if isinstance(found_raw, str) else None
        found = match.group(1) if match else "unknown"
        if found != recorded:
            raise BundleError(
                f"{tool} {found} differs from {recorded}, the version decision {name} "
                f"({decision.get('record')}) was made on; run {decision.get('probe')}, "
                "update the decision record and its version, then commit"
            )


def _json_files(paths: list[pathlib.Path]) -> list[dict]:
    return [_load_json(path) for path in paths if path.suffix == ".json"]


def _text(paths: list[pathlib.Path]) -> str:
    return "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in paths)


def _has_nonempty_raw_samples(value: object) -> bool:
    if isinstance(value, dict):
        samples = value.get("raw_samples")
        if isinstance(samples, list) and samples:
            return True
        return any(_has_nonempty_raw_samples(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_nonempty_raw_samples(item) for item in value)
    return False


def _check_selection_raw(paths: list[pathlib.Path]) -> None:
    owner_scenarios: dict[tuple[str, str], int] = {}
    scenario_totals: dict[str, int] = {}
    owners: set[str] = set()
    for report in _json_files(paths):
        if report.get("kind") != "selection_latency_raw":
            raise BundleError("selection raw evidence has wrong kind")
        privacy = report.get("privacy", {})
        if privacy.get("content_stored") is not False or privacy.get("content_hash_stored") is not False:
            raise BundleError("selection raw evidence violates content-retention contract")
        owner = str(report.get("owner_class", "")).strip()
        scenario = str(report.get("scenario", "")).strip()
        samples = report.get("samples")
        if not owner or not isinstance(samples, list):
            raise BundleError("selection raw evidence is incomplete")
        owners.add(owner)
        owner_scenarios[(owner, scenario)] = owner_scenarios.get((owner, scenario), 0) + len(samples)
        scenario_totals[scenario] = scenario_totals.get(scenario, 0) + len(samples)
    if len(owners) < 3:
        raise BundleError("selection dataset has fewer than three owner classes")
    for owner in owners:
        for scenario in ("primary-present", "clipboard-present"):
            if owner_scenarios.get((owner, scenario), 0) < 100:
                raise BundleError("selection dataset lacks 100 present samples per owner")
    for scenario in ("primary-absent", "clipboard-empty", "owner-exit"):
        if scenario_totals.get(scenario, 0) < 20:
            raise BundleError("selection dataset lacks required absent/exit samples")


def _check_selection_tradeoff(paths: list[pathlib.Path]) -> None:
    if not any(
        report.get("kind") == "selection_latency_tradeoff_report"
        and report.get("dataset_complete") is True
        and isinstance(report.get("candidate_timeouts"), list)
        for report in _json_files(paths)
    ):
        raise BundleError("selection trade-off report is incomplete")


def _check_benchmark(name: str):
    def check(paths: list[pathlib.Path]) -> None:
        if not any(
            report.get("schema_version") == 2 and _has_nonempty_raw_samples(report)
            for report in _json_files(paths)
        ):
            raise BundleError(f"{name} has no schema-2 raw samples")

    return check


def _check_giant_rss(paths: list[pathlib.Path]) -> None:
    # Schema 2 judges RSS against the CPython string size of each text kind
    # and records the UTF-8-based ratio and the change of definition.
    kinds = {"ascii", "cjk", "ascii_ipa", "ascii_astral"}
    if not any(
        report.get("schema_version") == 2
        and report.get("summary", {}).get("correctness_pass") is True
        and report.get("summary", {}).get("owner_approved") is True
        and report.get("summary", {}).get("rss_candidate_pass") is True
        and bool(report.get("threshold_definition"))
        and bool(report.get("previous_threshold_definition"))
        and {(sample.get("kind"), sample.get("size_mib")) for sample in report.get("raw_samples", [])}
        == {(kind, size) for kind in kinds for size in (1, 8, 32)}
        and all("utf8_ratio" in report.get("by_kind", {}).get(kind, {}) for kind in kinds)
        for report in _json_files(paths)
    ):
        raise BundleError(
            "giant RSS evidence lacks owner-approved passing 1/8/32 MiB samples for every text kind"
        )


def _check_wayland(label: str):
    def check(paths: list[pathlib.Path]) -> None:
        if not any(
            report.get("kind") == "real_wayland_owner_presentation_check"
            and report.get("offscreen") is False
            and report.get("owner_checks", {}).get("pass") is True
            for report in _json_files(paths)
        ):
            raise BundleError(f"{label} evidence has no passing real-session record")

    return check


def _check_percentiles(paths: list[pathlib.Path]) -> None:
    kinds = {
        item.get("kind")
        for report in _json_files(paths)
        if report.get("kind") == "benchmark_percentile_recalculation" and report.get("all_match") is True
        for item in report.get("inputs", [])
        if isinstance(item, dict)
    }
    if not {"backend", "qml"} <= kinds:
        raise BundleError("percentile recalculation evidence does not match")


def _check_verdict(label: str, needle: str):
    def check(paths: list[pathlib.Path]) -> None:
        if needle not in _text(paths):
            raise BundleError(f"{label} log does not contain {needle!r}")

    return check


# Product verdicts that every bundle must show. A group listed here must exist
# in the requirements file; tests/test_packaging.py checks the real file.
CONTENT_CHECKS = {
    "qml": _check_verdict("QML smoke", "QML_SMOKE: PASS"),
    "privacy": _check_verdict("privacy", "PRIVACY_TRANSPORT: PASS"),
    "pre_release": _check_verdict("pre-release", "All checks passed."),
    "backend_benchmark": _check_benchmark("backend benchmark"),
    "qml_benchmark": _check_benchmark("QML benchmark"),
    "percentiles": _check_percentiles,
    "giant_rss": _check_giant_rss,
    "selection_raw_latency": _check_selection_raw,
    "selection_tradeoff_report": _check_selection_tradeoff,
    "wayland_owner": _check_wayland("Wayland owner"),
    "dms_session_owner": _check_wayland("DMS session"),
}


def validate_evidence_content(groups: dict[str, list[pathlib.Path]]) -> None:
    for name, check in CONTENT_CHECKS.items():
        if name in groups:
            check(groups[name])


def _add_bytes(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(f"{ARCHIVE_ROOT}/{name}")
    info.size = len(data)
    info.mode = 0o644
    info.mtime = 0
    info.uname = info.gname = "root"
    archive.addfile(info, io.BytesIO(data))


def build_bundle(
    root: pathlib.Path,
    context_path: pathlib.Path,
    output: pathlib.Path,
    *,
    environment: dict[str, object] | None = None,
) -> dict[str, object]:
    root = root.resolve()
    output = output.resolve()
    if output == root or root in output.parents:
        raise BundleError("bundle output must be outside the repository")
    try:
        context = release_evidence.load_context(context_path)
        commit = str(context["commit"])
        requirements = release_evidence.load_requirements(root, commit)
        groups = release_evidence.verify_context(root, context)
    except release_evidence.ReleaseEvidenceError as exc:
        raise BundleError(str(exc)) from exc
    environment = capture_environment() if environment is None else environment
    check_decisions(environment, requirements.get("decisions", {}))
    validate_evidence_content(groups)

    evidence = context["evidence"]
    assert isinstance(evidence, dict)
    environment_bytes = (json.dumps(environment, indent=2, sort_keys=True) + "\n").encode("utf-8")
    binding = {
        "schema_version": BINDING_SCHEMA_VERSION,
        "kind": "dank_lookup_release_evidence_binding",
        "release_session_id": context["release_session_id"],
        "created_utc": dt.datetime.now(dt.UTC).isoformat(),
        "commit": commit,
        "tree": context["tree"],
        "source": "source/ is git archive of the commit; verify with the tree hash",
        "evidence": evidence,
        "groups": {name: sorted(p.relative_to(root).as_posix() for p in paths) for name, paths in groups.items()},
        "environment_sha256": hashlib.sha256(environment_bytes).hexdigest(),
    }
    binding_bytes = (json.dumps(binding, indent=2, sort_keys=True) + "\n").encode("utf-8")

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".dank-lookup-review-", dir=output.parent) as temporary:
        tar_path = pathlib.Path(temporary) / "review.tar"
        release_evidence.git(
            root, "archive", "--format=tar", f"--prefix={ARCHIVE_ROOT}/source/",
            "-o", os.fspath(tar_path), commit,
        )
        checksums: list[str] = []
        with tarfile.open(tar_path, "a", format=tarfile.PAX_FORMAT) as archive:
            for relative in sorted(evidence):
                data = (root / relative).read_bytes()
                if hashlib.sha256(data).hexdigest() != evidence[relative]["sha256"]:
                    raise BundleError(f"evidence changed while building the bundle: {relative}")
                _add_bytes(archive, f"evidence/{relative}", data)
                checksums.append(f"{evidence[relative]['sha256']}  evidence/{relative}")
            for name, data in (("environment.json", environment_bytes), ("binding.json", binding_bytes)):
                _add_bytes(archive, name, data)
                checksums.append(f"{hashlib.sha256(data).hexdigest()}  {name}")
            _add_bytes(archive, "checksums.sha256", ("\n".join(checksums) + "\n").encode("utf-8"))
        temporary_bundle = pathlib.Path(temporary) / "review.tar.gz"
        with tar_path.open("rb") as source, temporary_bundle.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                shutil.copyfileobj(source, compressed)
        os.replace(temporary_bundle, output)
    return binding


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-context", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args(argv)
    root = pathlib.Path(__file__).resolve().parent.parent
    try:
        binding = build_bundle(root, args.release_context, args.output)
    except BundleError as exc:
        print(f"build-review-bundle: {exc}", file=sys.stderr)
        return 1
    print(f"Review bundle created: {args.output}")
    print(f"commit {binding['commit']} tree {binding['tree']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
