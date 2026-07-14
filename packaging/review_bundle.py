#!/usr/bin/env python3
"""Build a manifest-verified, evidence-complete review archive."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import pathlib
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Iterable


ARCHIVE_ROOT = "dank-lookup-review"


class BundleError(RuntimeError):
    pass


def load_requirements(path: pathlib.Path) -> dict[str, list[str]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BundleError("invalid evidence requirements") from exc
    groups = value.get("required_groups") if isinstance(value, dict) else None
    if value.get("schema_version") != 1 or not isinstance(groups, dict):
        raise BundleError("unsupported evidence requirements schema")
    normalized: dict[str, list[str]] = {}
    for name, patterns in groups.items():
        if not isinstance(name, str) or not isinstance(patterns, list) or not patterns:
            raise BundleError("invalid evidence requirement group")
        if not all(isinstance(pattern, str) and pattern for pattern in patterns):
            raise BundleError("invalid evidence requirement pattern")
        normalized[name] = patterns
    return normalized


def missing_evidence_groups(
    root: pathlib.Path, groups: dict[str, list[str]]
) -> list[str]:
    missing: list[str] = []
    for name, patterns in groups.items():
        matches = {
            path
            for pattern in patterns
            for path in root.glob(pattern)
            if path.is_file() and not path.is_symlink()
        }
        if not matches:
            missing.append(name)
    return sorted(missing)


def matching_evidence(
    root: pathlib.Path, groups: dict[str, list[str]], name: str
) -> list[pathlib.Path]:
    return sorted(
        {
            path
            for pattern in groups[name]
            for path in root.glob(pattern)
            if path.is_file() and not path.is_symlink()
        }
    )


def _load_json(path: pathlib.Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BundleError(f"invalid JSON evidence type: {path.name}") from exc
    if not isinstance(value, dict):
        raise BundleError(f"invalid JSON evidence shape: {path.name}")
    return value


def _has_nonempty_raw_samples(value: object) -> bool:
    if isinstance(value, dict):
        if isinstance(value.get("raw_samples"), list) and value["raw_samples"]:
            return True
        return any(_has_nonempty_raw_samples(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_nonempty_raw_samples(item) for item in value)
    return False


def validate_evidence_content(
    root: pathlib.Path, groups: dict[str, list[str]]
) -> None:
    sdcv_reports = [
        _load_json(path) for path in matching_evidence(root, groups, "sdcv_gate_report")
    ]
    if not any(
        report.get("verdict") == "pass"
        and str(report.get("sdcv_version", "")).startswith("0.5.5")
        and not report.get("gate_errors")
        for report in sdcv_reports
    ):
        raise BundleError("sdcv gate evidence has no passing 0.5.5 verdict")

    socket_reports = [
        _load_json(path) for path in matching_evidence(root, groups, "socket_gate_report")
    ]
    if not any(
        report.get("decision") == "fixed_private_user_service_socket"
        and report.get("fixed_socket", {}).get("verdict") == "pass"
        for report in socket_reports
    ):
        raise BundleError("socket gate evidence has no passing fixed-socket decision")

    raw_selection = [
        _load_json(path) for path in matching_evidence(root, groups, "selection_raw_latency")
    ]
    owner_scenarios: dict[tuple[str, str], int] = {}
    scenario_totals: dict[str, int] = {}
    owners: set[str] = set()
    for report in raw_selection:
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

    tradeoffs = [
        _load_json(path)
        for path in matching_evidence(root, groups, "selection_tradeoff_report")
    ]
    if not any(
        report.get("kind") == "selection_latency_tradeoff_report"
        and report.get("dataset_complete") is True
        and isinstance(report.get("candidate_timeouts"), list)
        for report in tradeoffs
    ):
        raise BundleError("selection trade-off report is incomplete")

    for name in ("backend_raw_benchmark", "qml_raw_benchmark"):
        reports = [_load_json(path) for path in matching_evidence(root, groups, name)]
        if not any(report.get("schema_version") == 2 and _has_nonempty_raw_samples(report) for report in reports):
            raise BundleError(f"{name} has no schema-2 raw samples")

    giant_reports = [
        _load_json(path) for path in matching_evidence(root, groups, "giant_rss")
    ]
    if not any(
        report.get("summary", {}).get("correctness_pass") is True
        and report.get("summary", {}).get("owner_approved") is True
        and report.get("summary", {}).get("rss_candidate_pass") is True
        and {sample.get("size_mib") for sample in report.get("raw_samples", [])}
        == {1, 8, 32}
        for report in giant_reports
    ):
        raise BundleError("giant RSS evidence lacks owner-approved passing 1/8/32 MiB samples")

    wayland_reports = [
        _load_json(path) for path in matching_evidence(root, groups, "wayland_owner")
    ]
    if not any(
        report.get("kind") == "real_wayland_owner_presentation_check"
        and report.get("offscreen") is False
        and report.get("owner_checks", {}).get("pass") is True
        for report in wayland_reports
    ):
        raise BundleError("Wayland owner evidence has no passing real-session record")
    dms_reports = [
        _load_json(path)
        for path in matching_evidence(root, groups, "dms_session_owner")
    ]
    if not any(
        report.get("kind") == "real_wayland_owner_presentation_check"
        and report.get("offscreen") is False
        and report.get("owner_checks", {}).get("pass") is True
        for report in dms_reports
    ):
        raise BundleError("DMS real-session evidence has no passing owner record")

    recalculated = [
        _load_json(path)
        for path in matching_evidence(root, groups, "percentile_recalculation")
    ]
    matching_recalculations = [
        report
        for report in recalculated
        if report.get("kind") == "benchmark_percentile_recalculation"
        and report.get("all_match") is True
    ]
    recalculated_kinds = {
        item.get("kind")
        for report in matching_recalculations
        for item in report.get("inputs", [])
        if isinstance(item, dict)
    }
    if not {"backend", "qml"} <= recalculated_kinds:
        raise BundleError("percentile recalculation evidence does not match")

    privacy_logs = matching_evidence(root, groups, "privacy_logs")
    if not any("PRIVACY_TRANSPORT: PASS" in path.read_text(encoding="utf-8", errors="replace") for path in privacy_logs):
        raise BundleError("privacy log does not contain a passing sentinel verdict")
    qml_logs = matching_evidence(root, groups, "qml_logs")
    if not any("QML_SMOKE: PASS" in path.read_text(encoding="utf-8", errors="replace") for path in qml_logs):
        raise BundleError("QML log does not contain a passing smoke verdict")
    pre_release_logs = matching_evidence(root, groups, "pre_release_logs")
    if not any("All checks passed." in path.read_text(encoding="utf-8", errors="replace") for path in pre_release_logs):
        raise BundleError("pre-release log is not passing")


def git_candidate_paths(root: pathlib.Path) -> list[pathlib.PurePosixPath]:
    run = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if run.returncode != 0:
        raise BundleError("could not enumerate Git tracked/untracked candidates")
    result: list[pathlib.PurePosixPath] = []
    for raw in run.stdout.split(b"\0"):
        if not raw:
            continue
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BundleError("candidate path is not UTF-8") from exc
        path = pathlib.PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
            raise BundleError("unsafe Git candidate path")
        result.append(path)
    return sorted(set(result), key=lambda item: item.as_posix())


def filter_regular_candidates(
    root: pathlib.Path, candidates: Iterable[pathlib.PurePosixPath]
) -> list[pathlib.PurePosixPath]:
    regular: list[pathlib.PurePosixPath] = []
    for relative in candidates:
        path = root / relative.as_posix()
        try:
            info = path.lstat()
        except FileNotFoundError:
            # Deleted tracked entries are candidates, not archive members.
            continue
        if stat.S_ISREG(info.st_mode):
            regular.append(relative)
    return regular


def copy_candidates(
    root: pathlib.Path,
    staging: pathlib.Path,
    candidates: Iterable[pathlib.PurePosixPath],
) -> None:
    for relative in candidates:
        source = root / relative.as_posix()
        destination = staging / relative.as_posix()
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination, follow_symlinks=False)


def regular_staging_paths(staging: pathlib.Path) -> list[str]:
    return sorted(
        path.relative_to(staging).as_posix()
        for path in staging.rglob("*")
        if path.is_file() and not path.is_symlink()
    )


def write_review_metadata(staging: pathlib.Path, candidates: list[str]) -> None:
    metadata = staging / "review-metadata"
    metadata.mkdir(parents=True, exist_ok=True)
    (metadata / "source-candidates.txt").write_text(
        "".join(f"{item}\n" for item in candidates), encoding="utf-8"
    )
    (metadata / "build-command.txt").write_text(
        "packaging/build-review-bundle.sh --output PATH < privacy-sentinel-pipe\n",
        encoding="utf-8",
    )
    (metadata / "environment.json").write_text(
        json.dumps(
            {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "source_date_epoch": 0,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def finalize_staging_manifests(staging: pathlib.Path) -> list[str]:
    checksums_path = staging / "checksums.sha256"
    included_path = staging / "included-files.txt"
    source_files = [
        item
        for item in regular_staging_paths(staging)
        if item not in {"checksums.sha256", "included-files.txt"}
    ]
    checksums_path.write_text(
        "".join(f"{sha256_file(staging / item)}  {item}\n" for item in source_files),
        encoding="utf-8",
    )
    expected = sorted(source_files + ["checksums.sha256", "included-files.txt"])
    included_path.write_text("".join(f"{item}\n" for item in expected), encoding="utf-8")
    actual = regular_staging_paths(staging)
    if actual != expected:
        raise BundleError("staging manifest parity failed")
    return expected


def write_tar(staging: pathlib.Path, destination: pathlib.Path, expected: list[str]) -> None:
    with tarfile.open(destination, "w", format=tarfile.GNU_FORMAT) as archive:
        for relative in expected:
            source = staging / relative
            info = archive.gettarinfo(os.fspath(source), f"{ARCHIVE_ROOT}/{relative}")
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            info.mtime = 0
            with source.open("rb") as handle:
                archive.addfile(info, handle)
    with tarfile.open(destination, "r") as archive:
        members = archive.getmembers()
        if not all(member.isfile() for member in members):
            raise BundleError("archive contains a non-regular member")
        actual = sorted(
            member.name.removeprefix(f"{ARCHIVE_ROOT}/") for member in members
        )
    if actual != expected:
        raise BundleError("tar manifest parity failed")


def file_contains(path: pathlib.Path, needle: bytes) -> bool:
    overlap = max(0, len(needle) - 1)
    tail = b""
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            data = tail + chunk
            if needle in data:
                return True
            tail = data[-overlap:] if overlap else b""
    return False


def verify_sentinel_absent(
    staging: pathlib.Path,
    tar_path: pathlib.Path,
    bundle_path: pathlib.Path,
    sentinel: bytes,
) -> None:
    if len(sentinel) < 32:
        raise BundleError("privacy sentinel is too short")
    for relative in regular_staging_paths(staging):
        if sentinel in relative.encode("utf-8"):
            raise BundleError("privacy sentinel found in staged filename")
        if file_contains(staging / relative, sentinel):
            raise BundleError(f"privacy sentinel found in staged content type: {relative}")
    if sentinel in os.fspath(bundle_path).encode("utf-8"):
        raise BundleError("privacy sentinel found in bundle filename")
    if file_contains(tar_path, sentinel):
        raise BundleError("privacy sentinel found in tar metadata/content")
    if file_contains(bundle_path, sentinel):
        raise BundleError("privacy sentinel found in final bundle bytes")


def build_bundle(
    root: pathlib.Path,
    output: pathlib.Path,
    requirements_path: pathlib.Path,
    sentinel: bytes,
) -> None:
    groups = load_requirements(requirements_path)
    missing = missing_evidence_groups(root, groups)
    if missing:
        raise BundleError("missing mandatory evidence groups: " + ", ".join(missing))
    validate_evidence_content(root, groups)
    required_evidence = {
        path.relative_to(root).as_posix()
        for name in groups
        for path in matching_evidence(root, groups, name)
    }

    candidates = git_candidate_paths(root)
    regular = filter_regular_candidates(root, candidates)
    output = output.resolve()
    if sentinel in os.fspath(output).encode("utf-8"):
        raise BundleError("privacy sentinel found in requested bundle filename")
    try:
        output_relative = pathlib.PurePosixPath(output.relative_to(root).as_posix())
    except ValueError:
        output_relative = None
    if output_relative is not None:
        regular = [item for item in regular if item != output_relative]
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".dank-lookup-review-", dir=output.parent
    ) as temporary:
        staging = pathlib.Path(temporary) / ARCHIVE_ROOT
        staging.mkdir()
        copy_candidates(root, staging, regular)
        unstaged_evidence = sorted(
            relative
            for relative in required_evidence
            if not (staging / relative).is_file()
        )
        if unstaged_evidence:
            raise BundleError(
                "mandatory evidence is not a Git tracked/untracked regular candidate: "
                + ", ".join(unstaged_evidence)
            )
        write_review_metadata(staging, [item.as_posix() for item in candidates])
        expected = finalize_staging_manifests(staging)
        tar_path = pathlib.Path(temporary) / "review.tar"
        write_tar(staging, tar_path, expected)
        temporary_bundle = pathlib.Path(temporary) / "review.tar.gz"
        with tar_path.open("rb") as source, temporary_bundle.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                shutil.copyfileobj(source, compressed)
        verify_sentinel_absent(staging, tar_path, temporary_bundle, sentinel)
        os.replace(temporary_bundle, output)
        if file_contains(output, sentinel):
            output.unlink(missing_ok=True)
            raise BundleError("privacy sentinel found after final bundle move")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument(
        "--requirements",
        type=pathlib.Path,
        default=pathlib.Path(__file__).with_name("review-evidence-requirements.json"),
    )
    args = parser.parse_args()
    sentinel = sys.stdin.buffer.readline(4096).rstrip(b"\r\n")
    try:
        build_bundle(
            pathlib.Path(__file__).resolve().parent.parent,
            args.output,
            args.requirements,
            sentinel,
        )
    except BundleError as exc:
        print(f"build-review-bundle: {exc}", file=sys.stderr)
        return 1
    print(f"Review bundle created: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
