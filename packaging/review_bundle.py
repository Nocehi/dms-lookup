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
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Iterable

import release_evidence


ARCHIVE_ROOT = "dank-lookup-review"
SDCV_GATE_SCHEMA_VERSION = 2
SDCV_GATE_KIND = "sdcv_0_5_5_stdin_feasibility_gate"
SDCV_REQUIRED_VERSION = "0.5.5"
SDCV_STDERR_CAP_BYTES = 1024 * 1024
SDCV_VERSION_PATTERN = re.compile(
    r"\bversion\s+(\d+(?:\.\d+){2})(?![A-Za-z0-9_.+-])", re.IGNORECASE
)


class BundleError(RuntimeError):
    pass


def validate_bundle_output(
    root: pathlib.Path,
    output: pathlib.Path,
    protected_inputs: Iterable[pathlib.Path] = (),
) -> pathlib.Path:
    root = root.resolve(strict=True)
    output = output.resolve()
    if output == root or root in output.parents:
        raise BundleError("review bundle output must be outside repository root")
    for protected in protected_inputs:
        if output == protected.resolve():
            raise BundleError("review bundle output overlaps protected input")
    return output


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


def validate_sdcv_gate_report(report: dict) -> None:
    cases = report.get("cases")
    raw_version = report.get("sdcv_version_raw")
    version_match = (
        SDCV_VERSION_PATTERN.search(raw_version)
        if isinstance(raw_version, str)
        else None
    )
    stderr_over_cap = (
        [
            case
            for case in cases
            if isinstance(case, dict) and case.get("name") == "stderr_over_cap"
        ]
        if isinstance(cases, list)
        else []
    )
    valid_case = len(stderr_over_cap) == 1 and all(
        (
            stderr_over_cap[0].get("status") == "pass",
            stderr_over_cap[0].get("errors") == [],
            stderr_over_cap[0].get("stderr_cap_bytes") == SDCV_STDERR_CAP_BYTES,
            stderr_over_cap[0].get("termination_reason") == "stderr_cap",
            stderr_over_cap[0].get("stderr_bytes") == SDCV_STDERR_CAP_BYTES + 1,
            stderr_over_cap[0].get("stderr_stored_bytes")
            == SDCV_STDERR_CAP_BYTES,
            stderr_over_cap[0].get("process_group_reaped") is True,
        )
    )
    if not all(
        (
            report.get("schema_version") == SDCV_GATE_SCHEMA_VERSION,
            report.get("kind") == SDCV_GATE_KIND,
            report.get("verdict") == "pass",
            report.get("sdcv_version") == SDCV_REQUIRED_VERSION,
            version_match is not None
            and version_match.group(1) == SDCV_REQUIRED_VERSION,
            report.get("gate_errors") == [],
            report.get("stderr_cap_bytes") == SDCV_STDERR_CAP_BYTES,
            isinstance(cases, list) and bool(cases),
            valid_case,
        )
    ):
        raise BundleError("invalid sdcv 0.5.5 stdin gate evidence")


def validate_sdcv_gate_reports(paths: Iterable[pathlib.Path]) -> None:
    sdcv_reports = [_load_json(path) for path in paths]
    valid_sdcv_report = False
    for report in sdcv_reports:
        try:
            validate_sdcv_gate_report(report)
        except BundleError:
            continue
        valid_sdcv_report = True
        break
    if not valid_sdcv_report:
        raise BundleError("sdcv gate evidence has no passing 0.5.5 verdict")


def validate_evidence_content(evidence_by_group: dict[str, list[pathlib.Path]]) -> None:
    validate_sdcv_gate_reports(evidence_by_group["sdcv_gate_report"])

    socket_reports = [
        _load_json(path) for path in evidence_by_group["socket_gate_report"]
    ]
    if not any(
        report.get("decision") == "fixed_private_user_service_socket"
        and report.get("fixed_socket", {}).get("verdict") == "pass"
        for report in socket_reports
    ):
        raise BundleError("socket gate evidence has no passing fixed-socket decision")

    raw_selection = [
        _load_json(path) for path in evidence_by_group["selection_raw_latency"]
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
        for path in evidence_by_group["selection_tradeoff_report"]
    ]
    if not any(
        report.get("kind") == "selection_latency_tradeoff_report"
        and report.get("dataset_complete") is True
        and isinstance(report.get("candidate_timeouts"), list)
        for report in tradeoffs
    ):
        raise BundleError("selection trade-off report is incomplete")

    for name in ("backend_raw_benchmark", "qml_raw_benchmark"):
        reports = [_load_json(path) for path in evidence_by_group[name]]
        if not any(report.get("schema_version") == 2 and _has_nonempty_raw_samples(report) for report in reports):
            raise BundleError(f"{name} has no schema-2 raw samples")

    giant_reports = [
        _load_json(path) for path in evidence_by_group["giant_rss"]
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
        _load_json(path) for path in evidence_by_group["wayland_owner"]
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
        for path in evidence_by_group["dms_session_owner"]
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
        for path in evidence_by_group["percentile_recalculation"]
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

    privacy_logs = evidence_by_group["privacy_logs"]
    if not any("PRIVACY_TRANSPORT: PASS" in path.read_text(encoding="utf-8", errors="replace") for path in privacy_logs):
        raise BundleError("privacy log does not contain a passing sentinel verdict")
    qml_logs = evidence_by_group["qml_logs"]
    if not any("QML_SMOKE: PASS" in path.read_text(encoding="utf-8", errors="replace") for path in qml_logs):
        raise BundleError("QML log does not contain a passing smoke verdict")
    pre_release_logs = evidence_by_group["pre_release_logs"]
    if not any("All checks passed." in path.read_text(encoding="utf-8", errors="replace") for path in pre_release_logs):
        raise BundleError("pre-release log is not passing")


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


def write_review_metadata(
    staging: pathlib.Path,
    source_manifest: dict[str, object],
    release_session_id: str,
) -> None:
    metadata = staging / "review-metadata"
    metadata.mkdir(parents=True, exist_ok=True)
    (metadata / "source-candidates.txt").write_text(
        "".join(
            f"{item['path']}\n"
            for item in source_manifest["files"]
            if isinstance(item, dict)
        ),
        encoding="utf-8",
    )
    (metadata / "build-command.txt").write_text(
        "packaging/build-review-bundle.sh --release-context CONTEXT --output PATH\n",
        encoding="utf-8",
    )
    (metadata / "release-session.json").write_text(
        json.dumps(
            {
                "release_session_id": release_session_id,
                "source_manifest_schema_version": source_manifest["schema_version"],
                "source_digest": source_manifest["digest"],
                "source_digest_algorithm": source_manifest["algorithm"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
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


def stream_contains(handle, needle: bytes) -> bool:
    overlap = max(0, len(needle) - 1)
    tail = b""
    while chunk := handle.read(1024 * 1024):
        data = tail + chunk
        if needle in data:
            return True
        tail = data[-overlap:] if overlap else b""
    return False


def _archive_metadata_values(member: tarfile.TarInfo) -> Iterable[bytes]:
    for value in (member.name, member.linkname, member.uname, member.gname):
        if isinstance(value, str):
            yield value.encode("utf-8", errors="surrogateescape")
        elif isinstance(value, bytes):
            yield value
    for key, value in member.pax_headers.items():
        yield str(key).encode("utf-8", errors="surrogateescape")
        yield str(value).encode("utf-8", errors="surrogateescape")


def scan_archive_members(path: pathlib.Path, sentinel: bytes) -> None:
    """Reopen an archive and inspect resolved metadata and uncompressed members."""

    try:
        with tarfile.open(path, "r:*") as archive:
            for member in archive:
                if any(sentinel in value for value in _archive_metadata_values(member)):
                    raise BundleError(
                        f"privacy sentinel found in archive member metadata: {path.name}"
                    )
                if not member.isfile():
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise BundleError(f"could not extract archive member: {member.name}")
                with extracted:
                    if stream_contains(extracted, sentinel):
                        raise BundleError(
                            f"privacy sentinel found in archive member content: {path.name}"
                        )
    except (tarfile.TarError, OSError) as exc:
        raise BundleError(f"could not reopen review archive: {path.name}") from exc


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
    # Raw stream scans are supplementary. The member scans below are the
    # authoritative check for compressed content and archive metadata.
    if file_contains(tar_path, sentinel):
        raise BundleError("privacy sentinel found in tar metadata/content")
    if file_contains(bundle_path, sentinel):
        raise BundleError("privacy sentinel found in final bundle bytes")
    scan_archive_members(tar_path, sentinel)
    scan_archive_members(bundle_path, sentinel)


class SentinelPipeDispatcher:
    """Own one release sentinel and disclose it only over anonymous pipes."""

    def __init__(self) -> None:
        random_value = secrets.token_bytes(32)
        self._sentinel = bytearray(
            b"dank-lookup-privacy-" + random_value.hex().encode("ascii")
        )

    def _require_live(self) -> bytearray:
        if not self._sentinel:
            raise BundleError("privacy sentinel has already been discarded")
        return self._sentinel

    def assert_absent(self, value: bytes, description: str) -> None:
        if self._require_live() in value:
            raise BundleError(f"privacy sentinel found in {description}")

    def run(
        self,
        argv: list[str],
        *,
        cwd: pathlib.Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        sentinel = self._require_live()
        encoded_argv = b"\0".join(os.fsencode(item) for item in argv)
        self.assert_absent(encoded_argv, "child argv")
        child_env = os.environ.copy() if env is None else dict(env)
        encoded_env = b"\0".join(
            os.fsencode(key) + b"=" + os.fsencode(value)
            for key, value in child_env.items()
        )
        self.assert_absent(encoded_env, "child environment")

        if hasattr(os, "pipe2"):
            read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        else:
            read_fd, write_fd = os.pipe()
            os.set_inheritable(read_fd, False)
            os.set_inheritable(write_fd, False)
        child_argv = [*argv, "--sentinel-fd", str(read_fd)]
        try:
            process = subprocess.Popen(
                child_argv,
                cwd=cwd,
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(read_fd,),
            )
        except BaseException:
            os.close(read_fd)
            os.close(write_fd)
            raise
        os.close(read_fd)
        try:
            release_evidence.write_secret_fd(write_fd, sentinel)
        except BaseException:
            process.kill()
            process.communicate()
            raise
        stdout, stderr = process.communicate()
        self.assert_absent(stdout, "child stdout")
        self.assert_absent(stderr, "child stderr")
        return subprocess.CompletedProcess(child_argv, process.returncode, stdout, stderr)

    def discard(self) -> None:
        self._sentinel[:] = b"\0" * len(self._sentinel)
        self._sentinel.clear()


def _write_private_bytes(path: pathlib.Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def build_bundle(
    root: pathlib.Path,
    output: pathlib.Path,
    requirements_path: pathlib.Path,
    binding_path: pathlib.Path,
    sentinel: bytes,
) -> None:
    output = validate_bundle_output(
        root,
        output,
        (requirements_path, binding_path),
    )
    try:
        binding, selected_groups = release_evidence.verify_binding(
            root, requirements_path, binding_path
        )
    except release_evidence.ReleaseEvidenceError as exc:
        raise BundleError(str(exc)) from exc
    source_manifest = binding["source_manifest"]
    if not isinstance(source_manifest, dict):
        raise BundleError("release binding source manifest is invalid")
    source_entries = source_manifest.get("files")
    if not isinstance(source_entries, list):
        raise BundleError("release binding source candidate list is invalid")
    source_candidates = [
        pathlib.PurePosixPath(str(item["path"]))
        for item in source_entries
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    ]
    if len(source_candidates) != len(source_entries):
        raise BundleError("release binding source candidate entry is invalid")

    evidence_metadata = binding.get("evidence")
    if not isinstance(evidence_metadata, dict):
        raise BundleError("release binding evidence registry is invalid")
    evidence_candidates = [
        pathlib.PurePosixPath(relative) for relative in sorted(evidence_metadata)
    ]
    binding_relative = pathlib.PurePosixPath(
        binding_path.resolve().relative_to(root.resolve()).as_posix()
    )
    binding_digest = sha256_file(binding_path)
    if sentinel in os.fspath(output).encode("utf-8"):
        raise BundleError("privacy sentinel found in requested bundle filename")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".dank-lookup-review-", dir=output.parent
    ) as temporary:
        staging = pathlib.Path(temporary) / ARCHIVE_ROOT
        staging.mkdir()
        copy_candidates(root, staging, source_candidates)
        copy_candidates(root, staging, evidence_candidates)
        copy_candidates(root, staging, [binding_relative])
        try:
            release_evidence.verify_staged_source(staging, source_manifest)
            release_evidence.verify_recorded_evidence(staging, binding)
        except release_evidence.ReleaseEvidenceError as exc:
            raise BundleError(str(exc)) from exc
        if sha256_file(staging / binding_relative.as_posix()) != binding_digest:
            raise BundleError("staged release binding changed during copy")
        if _load_json(staging / binding_relative.as_posix()) != binding:
            raise BundleError("staged release binding content does not match verification")
        staged_groups = {
            name: [staging / path.relative_to(root) for path in paths]
            for name, paths in selected_groups.items()
        }
        validate_evidence_content(staged_groups)
        write_review_metadata(
            staging,
            source_manifest,
            str(binding["release_session_id"]),
        )
        expected = finalize_staging_manifests(staging)
        tar_path = pathlib.Path(temporary) / "review.tar"
        write_tar(staging, tar_path, expected)
        temporary_bundle = pathlib.Path(temporary) / "review.tar.gz"
        with tar_path.open("rb") as source, temporary_bundle.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                shutil.copyfileobj(source, compressed)
        verify_sentinel_absent(staging, tar_path, temporary_bundle, sentinel)
        try:
            current_manifest = release_evidence.canonical_source_manifest(root)
        except release_evidence.ReleaseEvidenceError as exc:
            raise BundleError(str(exc)) from exc
        if current_manifest != source_manifest:
            raise BundleError("current source changed while building review bundle")
        release_evidence.verify_recorded_evidence(root, binding)
        if (
            sha256_file(binding_path) != binding_digest
            or _load_json(binding_path) != binding
        ):
            raise BundleError("release binding changed while building review bundle")
        os.replace(temporary_bundle, output)
        try:
            scan_archive_members(output, sentinel)
        except BundleError:
            output.unlink(missing_ok=True)
            raise


def orchestrate_release_bundle(
    root: pathlib.Path,
    output: pathlib.Path,
    requirements_path: pathlib.Path,
    context_path: pathlib.Path,
) -> None:
    output = validate_bundle_output(
        root,
        output,
        (requirements_path, context_path),
    )
    try:
        context = release_evidence.load_context(context_path)
        release_evidence.verify_source_unchanged(root, context)
        release_evidence.verify_recorded_evidence(root, context)
        groups = release_evidence.load_requirements(requirements_path)
        missing = release_evidence.missing_recorded_groups(
            root,
            context,
            groups,
            exempt={"privacy_logs", "release_binding"},
        )
    except release_evidence.ReleaseEvidenceError as exc:
        raise BundleError(str(exc)) from exc
    for required_group in ("privacy_logs", "release_binding"):
        if required_group not in groups:
            raise BundleError(f"evidence requirements lack {required_group}")
    if missing:
        raise BundleError("missing bound evidence groups: " + ", ".join(missing))
    evidence = context.get("evidence")
    if not isinstance(evidence, dict):
        raise BundleError("release context evidence registry is invalid")
    recorded = set(evidence)
    prior_privacy = {
        path.relative_to(root).as_posix()
        for pattern in groups.get("privacy_logs", [])
        for path in root.glob(pattern)
        if path.is_file() and path.relative_to(root).as_posix() in recorded
    }
    if prior_privacy:
        raise BundleError("release context already contains privacy evidence")

    release_session_id = str(context["release_session_id"])
    privacy_log = (
        root / "tests/evidence/release/privacy" / f"{release_session_id}.log"
    )
    binding_path = (
        root / "tests/evidence/release/binding" / f"{release_session_id}.json"
    )
    if privacy_log.exists() or binding_path.exists():
        raise BundleError("release session output already exists")

    dispatcher = SentinelPipeDispatcher()
    try:
        verifier = dispatcher.run(
            [
                sys.executable,
                os.fspath(root / "tests/verify_privacy_transport.py"),
                "--scan-path",
                os.fspath(root),
            ],
            cwd=root,
        )
        if verifier.returncode != 0:
            raise BundleError(
                "privacy verifier failed:\n"
                + (verifier.stdout + verifier.stderr).decode("utf-8", errors="replace")
            )
        privacy_output = verifier.stdout + verifier.stderr
        if b"PRIVACY_TRANSPORT: PASS" not in privacy_output:
            raise BundleError("privacy verifier did not emit its passing verdict")
        dispatcher.assert_absent(privacy_output, "privacy evidence log")
        _write_private_bytes(privacy_log, privacy_output)
        try:
            release_evidence.record_evidence(root, context_path, [privacy_log])
            release_evidence.finalize_binding(
                root,
                context_path,
                requirements_path,
                binding_path,
            )
        except release_evidence.ReleaseEvidenceError as exc:
            raise BundleError(str(exc)) from exc
        dispatcher.assert_absent(binding_path.read_bytes(), "release evidence metadata")

        builder = dispatcher.run(
            [
                sys.executable,
                os.fspath(pathlib.Path(__file__).resolve()),
                "--internal-build",
                "--output",
                os.fspath(output),
                "--requirements",
                os.fspath(requirements_path),
                "--release-binding",
                os.fspath(binding_path),
            ],
            cwd=root,
        )
        if builder.stdout:
            sys.stdout.buffer.write(builder.stdout)
        if builder.stderr:
            sys.stderr.buffer.write(builder.stderr)
        if builder.returncode != 0:
            raise BundleError("review bundle builder failed")
    finally:
        dispatcher.discard()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--release-context", type=pathlib.Path)
    parser.add_argument("--release-binding", type=pathlib.Path, help=argparse.SUPPRESS)
    parser.add_argument("--sentinel-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--internal-build", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--requirements",
        type=pathlib.Path,
        default=pathlib.Path(__file__).with_name("review-evidence-requirements.json"),
    )
    args = parser.parse_args()
    try:
        root = pathlib.Path(__file__).resolve().parent.parent
        if args.internal_build:
            if args.release_binding is None or args.sentinel_fd is None:
                raise BundleError("internal builder requires binding and sentinel FD")
            sentinel = release_evidence.read_secret_fd(args.sentinel_fd)
            build_bundle(
                root,
                args.output,
                args.requirements,
                args.release_binding,
                sentinel,
            )
            print(f"Review bundle created: {args.output}")
        else:
            if (
                args.release_context is None
                or args.release_binding is not None
                or args.sentinel_fd is not None
            ):
                raise BundleError("release orchestration requires --release-context")
            orchestrate_release_bundle(
                root,
                args.output,
                args.requirements,
                args.release_context,
            )
    except (BundleError, release_evidence.ReleaseEvidenceError) as exc:
        print(f"build-review-bundle: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
