#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import signal
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest
from unittest import mock

try:
    import jsonschema
except ModuleNotFoundError:
    raise SystemExit(
        "missing pinned test dependency jsonschema; "
        "install requirements-dev.txt"
    ) from None


ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
sys.path.insert(0, str(PACKAGING))


SPEC = importlib.util.spec_from_file_location(
    "managed_files", PACKAGING / "managed-files.py"
)
assert SPEC is not None and SPEC.loader is not None
managed_files = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(managed_files)

BUNDLE_SPEC = importlib.util.spec_from_file_location(
    "review_bundle", PACKAGING / "review_bundle.py"
)
assert BUNDLE_SPEC is not None and BUNDLE_SPEC.loader is not None
review_bundle = importlib.util.module_from_spec(BUNDLE_SPEC)
BUNDLE_SPEC.loader.exec_module(review_bundle)

EVIDENCE_SPEC = importlib.util.spec_from_file_location(
    "release_evidence", PACKAGING / "release_evidence.py"
)
assert EVIDENCE_SPEC is not None and EVIDENCE_SPEC.loader is not None
release_evidence = importlib.util.module_from_spec(EVIDENCE_SPEC)
EVIDENCE_SPEC.loader.exec_module(release_evidence)

SDCV_PROBE_SPEC = importlib.util.spec_from_file_location(
    "probe_sdcv_stdin", ROOT / "tests/probe_sdcv_stdin.py"
)
assert SDCV_PROBE_SPEC is not None and SDCV_PROBE_SPEC.loader is not None
probe_sdcv_stdin = importlib.util.module_from_spec(SDCV_PROBE_SPEC)
SDCV_PROBE_SPEC.loader.exec_module(probe_sdcv_stdin)


class ManagedManifestUnitTests(unittest.TestCase):
    def test_path_traversal_and_unknown_roots_are_rejected(self):
        invalid_paths = ["/absolute", "../escape", "a/../escape", "a\\b", "a\tb"]
        for value in invalid_paths:
            with self.subTest(value=value), self.assertRaises(managed_files.ManifestError):
                managed_files.validate_relative_path(value)
        with self.assertRaises(managed_files.ManifestError):
            managed_files.validate_manifest(
                {"schema_version": 1, "roots": {"unknown": ["file"]}}
            )


class ReviewBundleUnitTests(unittest.TestCase):
    def _release_fixture(
        self,
        base: pathlib.Path,
        *,
        record_privacy: bool = True,
        external_requirements: bool = False,
        source_mode: int = 0o644,
    ) -> dict[str, pathlib.Path | dict]:
        root = base / "repo"
        root.mkdir()
        requirements = (
            base / "requirements.json"
            if external_requirements
            else root / "requirements.json"
        )
        requirements.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "required_groups": {
                        "unit_logs": ["tests/evidence/release/unit/*.log"],
                        "privacy_logs": ["tests/evidence/release/privacy/*.log"],
                        "release_binding": [
                            "tests/evidence/release/binding/*.json"
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )
        readme = root / "README.md"
        readme.write_text("tracked source\n", encoding="utf-8")
        readme.chmod(source_mode)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        tracked = ["README.md"]
        if not external_requirements:
            tracked.append("requirements.json")
        subprocess.run(["git", "add", *tracked], cwd=root, check=True)

        context_path = base / "release-context.json"
        context = release_evidence.create_context(root, context_path)
        unit = root / "tests/evidence/release/unit/unit.log"
        privacy = root / "tests/evidence/release/privacy/privacy.log"
        unit.parent.mkdir(parents=True)
        privacy.parent.mkdir(parents=True)
        unit.write_text("UNIT: PASS\n", encoding="utf-8")
        if record_privacy:
            privacy.write_text("PRIVACY_TRANSPORT: PASS\n", encoding="utf-8")
        recorded = [unit, privacy] if record_privacy else [unit]
        release_evidence.record_evidence(root, context_path, recorded)
        binding = (
            root
            / "tests/evidence/release/binding"
            / f"{context['release_session_id']}.json"
        )
        if record_privacy:
            release_evidence.finalize_binding(
                root,
                context_path,
                requirements,
                binding,
            )
        return {
            "root": root,
            "requirements": requirements,
            "readme": readme,
            "context_path": context_path,
            "context": context,
            "unit": unit,
            "privacy": privacy,
            "binding": binding,
        }

    @staticmethod
    def _snapshot(paths: list[pathlib.Path]) -> dict[pathlib.Path, bytes]:
        return {path: path.read_bytes() for path in paths}

    def test_bundle_rejects_repository_input_outputs_before_temporary_creation(self):
        for target_name in ("readme", "binding", "unit"):
            with self.subTest(target=target_name), tempfile.TemporaryDirectory() as directory:
                fixture = self._release_fixture(pathlib.Path(directory))
                root = fixture["root"]
                requirements = fixture["requirements"]
                binding = fixture["binding"]
                target = fixture[target_name]
                assert isinstance(root, pathlib.Path)
                assert isinstance(requirements, pathlib.Path)
                assert isinstance(binding, pathlib.Path)
                assert isinstance(target, pathlib.Path)
                inputs = [
                    fixture["readme"],
                    requirements,
                    fixture["context_path"],
                    fixture["unit"],
                    fixture["privacy"],
                    binding,
                ]
                assert all(isinstance(path, pathlib.Path) for path in inputs)
                before = self._snapshot(inputs)
                real_temporary_directory = tempfile.TemporaryDirectory
                with (
                    mock.patch.object(
                        review_bundle.tempfile,
                        "TemporaryDirectory",
                        wraps=real_temporary_directory,
                    ) as temporary_directory,
                    mock.patch.object(
                        review_bundle,
                        "validate_evidence_content",
                        return_value=None,
                    ),
                    self.assertRaisesRegex(
                        review_bundle.BundleError,
                        "outside repository|overlaps protected input",
                    ),
                ):
                    review_bundle.build_bundle(
                        root,
                        target,
                        requirements,
                        binding,
                        b"dank-lookup-privacy-" + (b"c" * 64),
                    )
                temporary_directory.assert_not_called()
                self.assertEqual(self._snapshot(inputs), before)
                self.assertFalse(target.read_bytes().startswith(b"\x1f\x8b"))

    def test_bundle_rejects_external_requirements_output_before_temporary_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._release_fixture(
                pathlib.Path(directory), external_requirements=True
            )
            root = fixture["root"]
            requirements = fixture["requirements"]
            binding = fixture["binding"]
            assert isinstance(root, pathlib.Path)
            assert isinstance(requirements, pathlib.Path)
            assert isinstance(binding, pathlib.Path)
            inputs = [
                fixture["readme"],
                requirements,
                fixture["context_path"],
                fixture["unit"],
                fixture["privacy"],
                binding,
            ]
            assert all(isinstance(path, pathlib.Path) for path in inputs)
            before = self._snapshot(inputs)
            real_temporary_directory = tempfile.TemporaryDirectory
            with (
                mock.patch.object(
                    review_bundle.tempfile,
                    "TemporaryDirectory",
                    wraps=real_temporary_directory,
                ) as temporary_directory,
                mock.patch.object(
                    review_bundle,
                    "validate_evidence_content",
                    return_value=None,
                ),
                self.assertRaisesRegex(
                    review_bundle.BundleError,
                    "overlaps protected input",
                ),
            ):
                review_bundle.build_bundle(
                    root,
                    requirements,
                    requirements,
                    binding,
                    b"dank-lookup-privacy-" + (b"d" * 64),
                )
            temporary_directory.assert_not_called()
            self.assertEqual(self._snapshot(inputs), before)
            self.assertFalse(requirements.read_bytes().startswith(b"\x1f\x8b"))

    def test_release_orchestrator_rejects_context_output_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._release_fixture(
                pathlib.Path(directory), record_privacy=False
            )
            root = fixture["root"]
            requirements = fixture["requirements"]
            context_path = fixture["context_path"]
            binding = fixture["binding"]
            privacy = fixture["privacy"]
            assert isinstance(root, pathlib.Path)
            assert isinstance(requirements, pathlib.Path)
            assert isinstance(context_path, pathlib.Path)
            assert isinstance(binding, pathlib.Path)
            assert isinstance(privacy, pathlib.Path)
            inputs = [
                fixture["readme"],
                requirements,
                context_path,
                fixture["unit"],
            ]
            assert all(isinstance(path, pathlib.Path) for path in inputs)
            before = self._snapshot(inputs)
            with (
                mock.patch.object(
                    review_bundle,
                    "SentinelPipeDispatcher",
                    side_effect=AssertionError("dispatcher reached before collision check"),
                ) as dispatcher,
                self.assertRaisesRegex(
                    review_bundle.BundleError,
                    "overlaps protected input",
                ),
            ):
                review_bundle.orchestrate_release_bundle(
                    root,
                    context_path,
                    requirements,
                    context_path,
                )
            dispatcher.assert_not_called()
            self.assertEqual(self._snapshot(inputs), before)
            self.assertFalse(context_path.read_bytes().startswith(b"\x1f\x8b"))
            self.assertFalse(binding.exists())
            self.assertFalse(privacy.exists())

    def test_source_manifest_hashes_current_paths_and_bytes_with_release_exclusions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            candidates = []

            def write(relative: str, value: str) -> pathlib.PurePosixPath:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value, encoding="utf-8")
                candidate = pathlib.PurePosixPath(relative)
                candidates.append(candidate)
                return candidate

            source = write("lib/source.py", "one")
            write(release_evidence.AUDIT_HANDOFF_NAME, "external audit")
            write("tests/evidence/release/unit/run.log", "generated evidence")
            write("build.log", "generated log")
            write("__pycache__/source.pyc", "cache")
            write("review.tar.gz", "archive")

            first = release_evidence.canonical_source_manifest(root, candidates)
            self.assertEqual(
                [item["path"] for item in first["files"]],
                [source.as_posix()],
            )

            (root / release_evidence.AUDIT_HANDOFF_NAME).write_text(
                "changed external audit", encoding="utf-8"
            )
            (root / "tests/evidence/release/unit/run.log").write_text(
                "changed evidence", encoding="utf-8"
            )
            self.assertEqual(
                release_evidence.canonical_source_manifest(root, candidates)["digest"],
                first["digest"],
            )
            (root / source.as_posix()).write_text("two", encoding="utf-8")
            self.assertNotEqual(
                release_evidence.canonical_source_manifest(root, candidates)["digest"],
                first["digest"],
            )

    def test_source_manifest_v2_binds_regular_type_and_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "tool"
            source.write_bytes(b"unchanged executable bytes\n")
            source.chmod(0o755)
            candidates = [pathlib.PurePosixPath("tool")]
            executable = release_evidence.canonical_source_manifest(root, candidates)
            source.chmod(0o644)
            non_executable = release_evidence.canonical_source_manifest(root, candidates)

            self.assertEqual(
                executable["files"][0]["sha256"],
                non_executable["files"][0]["sha256"],
            )
            self.assertNotEqual(executable["digest"], non_executable["digest"])
            self.assertEqual(executable["schema_version"], 2)
            self.assertEqual(
                executable["algorithm"],
                "sha256-regular-path-mode-size-and-bytes-v2",
            )
            self.assertEqual(executable["files"][0]["type"], "regular")
            self.assertEqual(executable["files"][0]["mode"], 0o755)
            self.assertEqual(non_executable["files"][0]["mode"], 0o644)

    def test_source_manifest_rejects_setid_and_nonregular_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "tool"
            source.write_text("source\n", encoding="utf-8")
            candidate = [pathlib.PurePosixPath("tool")]
            for mode in (0o4755, 0o2755):
                with self.subTest(mode=oct(mode)):
                    source.chmod(mode)
                    with self.assertRaisesRegex(
                        release_evidence.ReleaseEvidenceError,
                        "setuid|setgid",
                    ):
                        release_evidence.canonical_source_manifest(root, candidate)

            source.unlink()
            target = root / "target"
            target.write_text("target\n", encoding="utf-8")
            source.symlink_to(target)
            with self.subTest(kind="symlink"), self.assertRaisesRegex(
                release_evidence.ReleaseEvidenceError,
                "regular file",
            ):
                release_evidence.canonical_source_manifest(root, candidate)

            source.unlink()
            source.mkdir()
            with self.subTest(kind="directory"), self.assertRaisesRegex(
                release_evidence.ReleaseEvidenceError,
                "regular file",
            ):
                release_evidence.canonical_source_manifest(root, candidate)

    def test_mode_change_invalidates_existing_context_and_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._release_fixture(
                pathlib.Path(directory), source_mode=0o755
            )
            root = fixture["root"]
            readme = fixture["readme"]
            context_path = fixture["context_path"]
            requirements = fixture["requirements"]
            binding = fixture["binding"]
            assert isinstance(root, pathlib.Path)
            assert isinstance(readme, pathlib.Path)
            assert isinstance(context_path, pathlib.Path)
            assert isinstance(requirements, pathlib.Path)
            assert isinstance(binding, pathlib.Path)
            before_digest = hashlib.sha256(readme.read_bytes()).hexdigest()
            readme.chmod(0o644)
            self.assertEqual(
                hashlib.sha256(readme.read_bytes()).hexdigest(), before_digest
            )
            context = release_evidence.load_context(context_path)
            with self.assertRaisesRegex(
                release_evidence.ReleaseEvidenceError,
                "source candidates",
            ):
                release_evidence.verify_source_unchanged(root, context)
            with self.assertRaisesRegex(
                release_evidence.ReleaseEvidenceError,
                "source digest",
            ):
                release_evidence.verify_binding(root, requirements, binding)

    def test_release_binding_rejects_changed_or_mixed_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            root = base / "repo"
            root.mkdir()
            requirements = root / "requirements.json"
            requirements.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "required_groups": {
                            "unit_logs": ["tests/evidence/release/unit/*.log"],
                            "privacy_logs": ["tests/evidence/release/privacy/*.log"],
                            "release_binding": ["tests/evidence/release/binding/*.json"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            source = root / "source.py"
            source.write_text("release source", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", "source.py", "requirements.json"], cwd=root, check=True)

            context_path = base / "release-context.json"
            context = release_evidence.create_context(root, context_path)
            unit = root / "tests/evidence/release/unit/unit.log"
            privacy = root / "tests/evidence/release/privacy/privacy.log"
            unit.parent.mkdir(parents=True)
            privacy.parent.mkdir(parents=True)
            unit.write_text("UNIT: PASS\n", encoding="utf-8")
            privacy.write_text("PRIVACY_TRANSPORT: PASS\n", encoding="utf-8")
            release_evidence.record_evidence(root, context_path, [unit, privacy])
            binding_path = (
                root
                / "tests/evidence/release/binding"
                / f"{context['release_session_id']}.json"
            )
            release_evidence.finalize_binding(
                root,
                context_path,
                requirements,
                binding_path,
            )
            binding, selected = release_evidence.verify_binding(
                root,
                requirements,
                binding_path,
            )
            self.assertEqual(binding["release_session_id"], context["release_session_id"])
            self.assertEqual(selected["unit_logs"], [unit])
            self.assertEqual(
                binding["evidence"][unit.relative_to(root).as_posix()][
                    "release_session_id"
                ],
                context["release_session_id"],
            )
            self.assertEqual(
                binding["evidence"][unit.relative_to(root).as_posix()][
                    "source_digest"
                ],
                context["source_manifest"]["digest"],
            )

            unit.write_text("UNIT: changed after binding\n", encoding="utf-8")
            with self.assertRaisesRegex(
                release_evidence.ReleaseEvidenceError,
                "recorded evidence changed",
            ):
                release_evidence.verify_binding(root, requirements, binding_path)

            unit.write_text("UNIT: PASS\n", encoding="utf-8")
            other_context_path = base / "other-context.json"
            release_evidence.create_context(root, other_context_path)
            release_evidence.record_evidence(root, other_context_path, [unit])
            other_binding = root / "tests/evidence/release/binding/other.json"
            with self.assertRaisesRegex(
                release_evidence.ReleaseEvidenceError,
                "missing bound evidence groups: privacy_logs",
            ):
                release_evidence.finalize_binding(
                    root,
                    other_context_path,
                    requirements,
                    other_binding,
                )

    def test_staging_source_must_match_bound_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "source.py").write_text("before", encoding="utf-8")
            manifest = release_evidence.canonical_source_manifest(
                root, [pathlib.PurePosixPath("source.py")]
            )
            release_evidence.verify_staged_source(root, manifest)
            (root / "source.py").write_text("after", encoding="utf-8")
            with self.assertRaisesRegex(
                release_evidence.ReleaseEvidenceError,
                "staging source digest",
            ):
                release_evidence.verify_staged_source(root, manifest)

    def test_staging_source_mode_must_match_bound_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            source_root = base / "source"
            staging = base / "staging"
            source_root.mkdir()
            staging.mkdir()
            source = source_root / "tool"
            source.write_bytes(b"same staged bytes\n")
            source.chmod(0o755)
            manifest = release_evidence.canonical_source_manifest(
                source_root, [pathlib.PurePosixPath("tool")]
            )
            shutil.copy2(source, staging / "tool")
            release_evidence.verify_staged_source(staging, manifest)
            (staging / "tool").chmod(0o644)
            with self.assertRaisesRegex(
                release_evidence.ReleaseEvidenceError,
                "staging source digest",
            ):
                release_evidence.verify_staged_source(staging, manifest)

    def test_one_orchestrator_sentinel_is_reused_only_through_anonymous_fds(self):
        child = """
import hashlib
import os
import struct
import sys

fd = int(sys.argv[sys.argv.index('--sentinel-fd') + 1])
size = struct.unpack('>I', os.read(fd, 4))[0]
value = b''
while len(value) < size:
    value += os.read(fd, size - len(value))
argv_leak = any(value in item.encode() for item in sys.argv)
env_leak = any(value in item.encode() for item in os.environ.values())
print(hashlib.sha256(value).hexdigest(), int(argv_leak), int(env_leak))
"""
        random_value = b"R" * 32
        expected = b"dank-lookup-privacy-" + random_value.hex().encode("ascii")
        with mock.patch.object(
            review_bundle.secrets,
            "token_bytes",
            return_value=random_value,
        ) as generate:
            dispatcher = review_bundle.SentinelPipeDispatcher()
            first = dispatcher.run([sys.executable, "-c", child], cwd=ROOT)
            second = dispatcher.run([sys.executable, "-c", child], cwd=ROOT)
            dispatcher.discard()
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(first.returncode, 0, first.stderr.decode())
        self.assertEqual(second.returncode, 0, second.stderr.decode())
        expected_line = f"{hashlib.sha256(expected).hexdigest()} 0 0\n".encode()
        self.assertEqual(first.stdout, expected_line)
        self.assertEqual(second.stdout, expected_line)

    def test_archive_scan_checks_names_metadata_and_uncompressed_contents(self):
        sentinel = b"dank-lookup-privacy-" + (b"a" * 64)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)

            cases = {
                "name": (f"safe/{sentinel.decode()}", {}, b"safe"),
                "metadata": ("safe/member", {"comment": sentinel.decode()}, b"safe"),
                "content": ("safe/member", {}, b"before-" + sentinel + b"-after"),
            }
            for case, (name, pax_headers, content) in cases.items():
                with self.subTest(case=case):
                    archive_path = root / f"{case}.tar.gz"
                    with tarfile.open(
                        archive_path,
                        "w:gz",
                        format=tarfile.PAX_FORMAT,
                    ) as archive:
                        info = tarfile.TarInfo(name)
                        info.size = len(content)
                        info.pax_headers = pax_headers
                        archive.addfile(info, io.BytesIO(content))
                    self.assertFalse(
                        review_bundle.file_contains(archive_path, sentinel),
                        "test must not pass through a compressed-stream match",
                    )
                    with self.assertRaisesRegex(
                        review_bundle.BundleError,
                        "archive member (metadata|content)",
                    ):
                        review_bundle.scan_archive_members(archive_path, sentinel)

    def test_bundle_stages_only_bound_source_and_evidence_not_external_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            root = base / "repo"
            root.mkdir()
            requirements = root / "requirements.json"
            requirements.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "required_groups": {
                            "unit_logs": ["tests/evidence/release/unit/*.log"],
                            "privacy_logs": ["tests/evidence/release/privacy/*.log"],
                            "release_binding": [
                                "tests/evidence/release/binding/*.json"
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )
            (root / "source.py").write_text("candidate\n", encoding="utf-8")
            (root / release_evidence.AUDIT_HANDOFF_NAME).write_text(
                "external untracked handoff\n", encoding="utf-8"
            )
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "add", "source.py", "requirements.json"],
                cwd=root,
                check=True,
            )
            context_path = base / "release-context.json"
            context = release_evidence.create_context(root, context_path)
            unit = root / "tests/evidence/release/unit/unit.log"
            privacy = root / "tests/evidence/release/privacy/privacy.log"
            historical = root / "tests/evidence/release/unit/historical.log"
            unit.parent.mkdir(parents=True)
            privacy.parent.mkdir(parents=True)
            unit.write_text("UNIT: PASS\n", encoding="utf-8")
            privacy.write_text("PRIVACY_TRANSPORT: PASS\n", encoding="utf-8")
            historical.write_text("OLD: PASS\n", encoding="utf-8")
            release_evidence.record_evidence(root, context_path, [unit, privacy])
            binding = (
                root
                / "tests/evidence/release/binding"
                / f"{context['release_session_id']}.json"
            )
            release_evidence.finalize_binding(
                root, context_path, requirements, binding
            )
            output = base / "review.tar.gz"
            sentinel = b"dank-lookup-privacy-" + (b"b" * 64)
            with mock.patch.object(
                review_bundle,
                "validate_evidence_content",
                return_value=None,
            ):
                review_bundle.build_bundle(
                    root,
                    output,
                    requirements,
                    binding,
                    sentinel,
                )
            with tarfile.open(output, "r:gz") as archive:
                names = {member.name for member in archive.getmembers()}
                source_candidates = archive.extractfile(
                    f"{review_bundle.ARCHIVE_ROOT}/review-metadata/source-candidates.txt"
                )
                assert source_candidates is not None
                source_candidate_text = source_candidates.read().decode("utf-8")
            self.assertFalse(
                any(release_evidence.AUDIT_HANDOFF_NAME in name for name in names)
            )
            self.assertNotIn(release_evidence.AUDIT_HANDOFF_NAME, source_candidate_text)
            self.assertFalse(any("historical.log" in name for name in names))
            self.assertIn(
                f"{review_bundle.ARCHIVE_ROOT}/{unit.relative_to(root).as_posix()}",
                names,
            )
            self.assertIn(
                f"{review_bundle.ARCHIVE_ROOT}/{binding.relative_to(root).as_posix()}",
                names,
            )

    def test_deleted_and_symlink_candidates_are_filtered_before_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "present.txt").write_text("present", encoding="utf-8")
            (root / "link.txt").symlink_to(root / "present.txt")
            candidates = [
                pathlib.PurePosixPath("deleted.txt"),
                pathlib.PurePosixPath("link.txt"),
                pathlib.PurePosixPath("present.txt"),
            ]
            self.assertEqual(
                review_bundle.filter_regular_candidates(root, candidates),
                [pathlib.PurePosixPath("present.txt")],
            )

    def test_staging_and_tar_manifests_are_bidirectionally_equal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            staging = root / "stage"
            staging.mkdir()
            (staging / "nested").mkdir()
            (staging / "nested/evidence.json").write_text("{}\n", encoding="utf-8")
            expected = review_bundle.finalize_staging_manifests(staging)
            archive = root / "review.tar"
            review_bundle.write_tar(staging, archive, expected)
            self.assertEqual(review_bundle.regular_staging_paths(staging), expected)
            with tarfile.open(archive, "r") as handle:
                actual = sorted(
                    member.name.removeprefix(
                        f"{review_bundle.ARCHIVE_ROOT}/"
                    )
                    for member in handle.getmembers()
                )
            self.assertEqual(actual, expected)

    def test_missing_evidence_groups_are_named(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "present.log").write_text("ok", encoding="utf-8")
            missing = review_bundle.missing_evidence_groups(
                root,
                {"present": ["present.log"], "missing": ["missing/*.json"]},
            )
            self.assertEqual(missing, ["missing"])

    def test_duplicate_and_schema_validation(self):
        with self.assertRaises(managed_files.ManifestError):
            managed_files.validate_manifest(
                {"schema_version": 2, "roots": {"home_bin": []}}
            )
        with self.assertRaises(managed_files.ManifestError):
            managed_files.validate_manifest(
                {
                    "schema_version": 1,
                    "roots": {"home_bin": ["same", "same"]},
                }
            )


class SdcvGateEvidenceTests(unittest.TestCase):
    STDERR_CAP_BYTES = 1024 * 1024
    STDOUT_CAP_BYTES = 64 * 1024 * 1024
    SCHEMA_PATH = ROOT / "tests/fixtures-sdcv-stdin/evidence-schema.json"
    CASE_NAMES = (
        "exact_hit",
        "exact_miss",
        "fuzzy_choice",
        "single_dictionary",
        "multiple_dictionaries",
        "unicode_en",
        "unicode_han",
        "unicode_ja",
        "unicode_yue",
        "stdin_eof_after_exact",
        "stdin_eof_at_main_prompt",
        "source_timeout",
        "overall_timeout",
        "cancellation",
        "stdout_under_cap_32mib",
        "stdout_over_cap",
        "stderr_over_cap",
        "sentinel_cmdline",
    )

    @classmethod
    def setUpClass(cls) -> None:
        schema = json.loads(cls.SCHEMA_PATH.read_text(encoding="utf-8"))
        validator_class = jsonschema.validators.validator_for(schema)
        validator_class.check_schema(schema)
        cls.schema_contract = schema
        cls.schema_validator = validator_class(schema)

    def _case(self, name: str) -> dict:
        termination_reason = None
        returncode = 0
        stdout_bytes = 0
        stdout_stored_bytes = 0
        stderr_bytes = 0
        stderr_stored_bytes = 0
        runner_decisions = []
        if name in {"source_timeout", "overall_timeout"}:
            termination_reason = "timeout"
            returncode = -9
            runner_decisions = ["timeout"]
        elif name == "cancellation":
            termination_reason = "cancelled"
            returncode = -9
            runner_decisions = ["query_sent", "cancelled"]
        elif name == "stdout_under_cap_32mib":
            stdout_bytes = 32 * 1024 * 1024 + 1
            stdout_stored_bytes = stdout_bytes
        elif name == "stdout_over_cap":
            termination_reason = "stdout_cap"
            returncode = -9
            stdout_bytes = self.STDOUT_CAP_BYTES + 1
            stdout_stored_bytes = self.STDOUT_CAP_BYTES
            runner_decisions = ["stdout_cap"]
        elif name == "stderr_over_cap":
            termination_reason = "stderr_cap"
            returncode = -9
            stderr_bytes = self.STDERR_CAP_BYTES + 1
            stderr_stored_bytes = self.STDERR_CAP_BYTES
            runner_decisions = ["stderr_cap"]
        elif name == "sentinel_cmdline":
            runner_decisions = ["query_sent"]
        result = {
            "name": name,
            "argv_metadata": ["--json"],
            "query_bytes": 56 if name == "sentinel_cmdline" else 0,
            "choice": None,
            "pid": 1234,
            "returncode": returncode,
            "duration_ms": 1.0,
            "termination_reason": termination_reason,
            "stdout_bytes": stdout_bytes,
            "stdout_stored_bytes": stdout_stored_bytes,
            "stderr_bytes": stderr_bytes,
            "stderr_stored_bytes": stderr_stored_bytes,
            "stdout_cap_bytes": self.STDOUT_CAP_BYTES,
            "stderr_cap_bytes": self.STDERR_CAP_BYTES,
            "stdout_file": f"{name}.stdout.bin",
            "stderr_file": f"{name}.stderr.bin",
            "cmdline_file": f"{name}.cmdline.bin" if name == "sentinel_cmdline" else None,
            "query_in_cmdline": False,
            "main_prompt_count": 0,
            "choice_prompt_count": 0,
            "process_group_reaped": True,
            "runner_decisions": runner_decisions,
            "errors": [],
            "status": "pass",
        }
        if name == "sentinel_cmdline":
            result["sentinel_environment_match"] = False
        return result

    def _sdcv_report(
        self,
        *,
        version_returncode: int = 0,
        version_raw: str = "Console version of Stardict, version 0.5.5",
    ) -> dict:
        return probe_sdcv_stdin.build_gate_report(
            version_returncode=version_returncode,
            version_raw=version_raw,
            history_paths=[],
            cases=[self._case(name) for name in self.CASE_NAMES],
        )

    def _validate_schema(self, report: dict) -> None:
        self.schema_validator.validate(report)

    @staticmethod
    def _safe_fixture_name(value: object) -> bool:
        if not isinstance(value, str) or not value:
            return False
        relative = pathlib.PurePosixPath(value)
        return not relative.is_absolute() and len(relative.parts) == 1

    def _write_report_artifacts(self, root: pathlib.Path, report: dict) -> None:
        cases = report.get("cases")
        if not isinstance(cases, list):
            return
        for case in cases:
            if not isinstance(case, dict):
                continue
            for stream in ("stdout", "stderr"):
                filename = case.get(f"{stream}_file")
                size = case.get(f"{stream}_stored_bytes")
                if not self._safe_fixture_name(filename):
                    continue
                if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                    continue
                path = root / filename
                with path.open("wb") as handle:
                    handle.truncate(size)
            cmdline_file = case.get("cmdline_file")
            if self._safe_fixture_name(cmdline_file):
                metadata = case.get("argv_metadata")
                if not isinstance(metadata, list) or not all(
                    isinstance(item, str) for item in metadata
                ):
                    metadata = []
                argv = ["/usr/bin/sdcv", *metadata]
                (root / cmdline_file).write_bytes(
                    b"\0".join(item.encode("utf-8") for item in argv) + b"\0"
                )

    def _write_report(
        self,
        root: pathlib.Path,
        report: dict,
        *,
        raw_json: str | None = None,
    ) -> pathlib.Path:
        root.mkdir(parents=True, exist_ok=True)
        self._write_report_artifacts(root, report)
        path = root / "report.json"
        path.write_text(
            raw_json
            if raw_json is not None
            else json.dumps(report, ensure_ascii=False, allow_nan=False),
            encoding="utf-8",
        )
        return path

    def _validate(self, report: dict) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_report(pathlib.Path(directory), report)
            review_bundle.validate_sdcv_gate_reports([path])

    def _validate_group(self, reports: list[dict]) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            paths = [
                self._write_report(root / f"report-{index}", report)
                for index, report in enumerate(reports)
            ]
            review_bundle.validate_sdcv_gate_reports(paths)

    def _assert_schema_invalid_and_consumer_rejects(self, report: dict) -> None:
        with self.assertRaises(jsonschema.exceptions.ValidationError):
            self._validate_schema(report)
        with self.assertRaises(review_bundle.BundleError):
            self._validate(report)

    def _assert_schema_valid_and_consumer_rejects(self, report: dict) -> None:
        self._validate_schema(report)
        with self.assertRaises(review_bundle.BundleError):
            self._validate(report)

    def test_producer_version_parser_normalizes_three_part_versions(self):
        for version, expected in (
            ("0.5.5", "0.5.5"),
            ("0.5.50", "0.5.50"),
            ("0.5.5.1", ""),
            ("0.5.5-beta", ""),
            ("0.5.5+meta", ""),
        ):
            with self.subTest(version=version):
                self.assertEqual(
                    probe_sdcv_stdin.parse_sdcv_version(
                        f"Console version of Stardict, version {version}"
                    ),
                    expected,
                )

    def test_producer_pass_report_matches_schema_and_consumer(self):
        report = self._sdcv_report()
        self.assertEqual(report["verdict"], "pass")
        self.assertEqual(report["sdcv_version"], "0.5.5")
        self.assertEqual(report["gate_errors"], [])
        self._validate_schema(report)
        self._validate(report)

    def test_producer_fail_report_matches_schema_and_consumer_rejects(self):
        report = self._sdcv_report(
            version_raw="Console version of Stardict, version 0.5.6"
        )
        self.assertEqual(report["verdict"], "fail")
        self.assertEqual(report["sdcv_version"], "0.5.6")
        self.assertEqual(
            report["gate_errors"],
            ["installed sdcv is not the required 0.5.5 binary"],
        )
        self._validate_schema(report)
        with self.assertRaises(review_bundle.BundleError):
            self._validate(report)

    def test_producer_empty_version_failure_is_schema_valid_but_not_passing(self):
        report = self._sdcv_report(version_returncode=1, version_raw="")
        self.assertEqual(report["verdict"], "fail")
        self.assertEqual(report["sdcv_version"], "")
        self.assertEqual(report["sdcv_version_raw"], "")
        self.assertEqual(report["sdcv_version_returncode"], 1)
        self.assertEqual(
            report["gate_errors"],
            ["installed sdcv is not the required 0.5.5 binary"],
        )
        self._validate_schema(report)
        self._validate_group([self._sdcv_report(), report])
        with self.assertRaisesRegex(review_bundle.BundleError, "no passing"):
            self._validate(report)

    def test_passing_report_requires_successful_nonempty_exact_version_capture(self):
        empty_raw = self._sdcv_report()
        empty_raw["sdcv_version_raw"] = ""
        self._assert_schema_invalid_and_consumer_rejects(empty_raw)

        for field, value in (
            ("sdcv_version_returncode", 1),
            ("sdcv_version", ""),
        ):
            with self.subTest(field=field):
                report = self._sdcv_report()
                report[field] = value
                self._validate_schema(report)
                with self.assertRaises(review_bundle.BundleError):
                    self._validate(report)

    def test_producer_case_failure_matches_schema_and_consumer_rejects(self):
        cases = [self._case(name) for name in self.CASE_NAMES]
        cases[0]["errors"] = ["forced producer failure"]
        cases[0]["status"] = "fail"
        report = probe_sdcv_stdin.build_gate_report(
            version_returncode=0,
            version_raw="Console version of Stardict, version 0.5.5",
            history_paths=[],
            cases=cases,
        )
        self.assertEqual(report["verdict"], "fail")
        self.assertEqual(report["gate_errors"], ["failed cases: exact_hit"])
        self._validate_schema(report)
        with self.assertRaises(review_bundle.BundleError):
            self._validate(report)

    def test_production_consumer_rejects_schema_invalid_incomplete_report(self):
        report = {
            "schema_version": 2,
            "kind": "sdcv_0_5_5_stdin_feasibility_gate",
            "verdict": "pass",
            "sdcv_version": "0.5.5",
            "sdcv_version_raw": "Console version of Stardict, version 0.5.5",
            "stderr_cap_bytes": self.STDERR_CAP_BYTES,
            "gate_errors": [],
            "cases": [self._case("stderr_over_cap")],
        }
        with self.assertRaises(jsonschema.exceptions.ValidationError):
            self._validate_schema(report)
        with self.assertRaises(review_bundle.BundleError):
            self._validate(report)

    def test_required_case_set_matches_schema_and_consumer(self):
        schema_names = tuple(
            self.schema_contract["$defs"]["case"]["properties"]["name"]["enum"]
        )
        self.assertEqual(schema_names, self.CASE_NAMES)

        for missing_name in self.CASE_NAMES:
            with self.subTest(kind="missing", case=missing_name):
                report = self._sdcv_report()
                report["cases"] = [
                    case for case in report["cases"] if case["name"] != missing_name
                ]
                self._assert_schema_invalid_and_consumer_rejects(report)

            with self.subTest(kind="duplicate", case=missing_name):
                report = self._sdcv_report()
                duplicate = next(
                    case for case in report["cases"] if case["name"] != missing_name
                )
                report["cases"] = [
                    case for case in report["cases"] if case["name"] != missing_name
                ]
                report["cases"].append(copy.deepcopy(duplicate))
                self._assert_schema_invalid_and_consumer_rejects(report)

    def test_missing_required_fields_are_rejected_by_schema_and_consumer(self):
        for field in self.schema_contract["required"]:
            with self.subTest(scope="report", field=field):
                report = self._sdcv_report()
                del report[field]
                self._assert_schema_invalid_and_consumer_rejects(report)

        case_required = self.schema_contract["$defs"]["case"]["required"]
        for field in case_required:
            with self.subTest(scope="case", field=field):
                report = self._sdcv_report()
                del report["cases"][0][field]
                self._assert_schema_invalid_and_consumer_rejects(report)

        report = self._sdcv_report()
        sentinel = next(
            case for case in report["cases"] if case["name"] == "sentinel_cmdline"
        )
        del sentinel["sentinel_environment_match"]
        self._assert_schema_invalid_and_consumer_rejects(report)

    def test_schema_invalid_extra_and_malformed_data_are_rejected_in_production(self):
        extra_report = self._sdcv_report()
        extra_report["unexpected"] = True
        extra_case = self._sdcv_report()
        extra_case["cases"][0]["unexpected"] = True
        malformed_cases = self._sdcv_report()
        malformed_cases["cases"] = {"not": "an array"}
        malformed_parser = self._sdcv_report()
        malformed_parser["cases"][0]["parser"] = {"unexpected": True}

        for name, report in (
            ("extra_report", extra_report),
            ("extra_case", extra_case),
            ("malformed_cases", malformed_cases),
            ("malformed_parser", malformed_parser),
        ):
            with self.subTest(name=name):
                self._assert_schema_invalid_and_consumer_rejects(report)

    def test_valid_report_does_not_mask_schema_invalid_report(self):
        valid = self._sdcv_report()
        malformed = self._sdcv_report()
        malformed["unexpected"] = True
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            valid_path = self._write_report(root / "valid", valid)
            malformed_path = self._write_report(root / "malformed", malformed)
            with self.assertRaises(review_bundle.BundleError):
                review_bundle.validate_sdcv_gate_reports(
                    [valid_path, malformed_path]
                )

    def test_valid_pass_does_not_mask_schema_valid_contradictory_failure(self):
        valid = self._sdcv_report()
        contradictory = self._sdcv_report()
        contradictory["verdict"] = "fail"
        self._validate_schema(contradictory)
        with self.assertRaises(review_bundle.BundleError):
            self._validate_group([valid, contradictory])

    def test_coherent_failure_is_validated_before_pass_selection(self):
        cases = [self._case(name) for name in self.CASE_NAMES]
        cases[0]["errors"] = ["forced producer failure"]
        cases[0]["status"] = "fail"
        failure = probe_sdcv_stdin.build_gate_report(
            version_returncode=0,
            version_raw="Console version of Stardict, version 0.5.5",
            history_paths=[],
            cases=cases,
        )
        self._validate_schema(failure)
        self._validate_group([self._sdcv_report(), failure])
        with self.assertRaisesRegex(review_bundle.BundleError, "no passing"):
            self._validate_group([failure])

    def test_group_rejects_case_status_error_and_gate_error_contradictions(self):
        status_fail_without_errors = self._sdcv_report()
        status_fail_without_errors["cases"][0]["status"] = "fail"

        status_pass_with_errors = self._sdcv_report()
        status_pass_with_errors["cases"][0]["errors"] = ["hidden case failure"]

        wrong_failed_case_name = self._sdcv_report()
        wrong_failed_case_name["cases"][0]["status"] = "fail"
        wrong_failed_case_name["cases"][0]["errors"] = ["forced producer failure"]
        wrong_failed_case_name["verdict"] = "fail"
        wrong_failed_case_name["gate_errors"] = ["failed cases: exact_miss"]

        for name, contradictory in (
            ("fail_status_without_errors", status_fail_without_errors),
            ("pass_status_with_errors", status_pass_with_errors),
            ("wrong_failed_case_name", wrong_failed_case_name),
        ):
            with self.subTest(name=name):
                self._validate_schema(contradictory)
                with self.assertRaises(review_bundle.BundleError):
                    self._validate_group([self._sdcv_report(), contradictory])

    def test_schema_valid_history_and_privacy_mismatches_are_rejected(self):
        history = self._sdcv_report()
        history["history_paths"] = ["home/.sdcv_history"]
        query_in_cmdline = self._sdcv_report()
        query_in_cmdline["cases"][0]["query_in_cmdline"] = True
        sentinel_environment = self._sdcv_report()
        sentinel = next(
            case
            for case in sentinel_environment["cases"]
            if case["name"] == "sentinel_cmdline"
        )
        sentinel["sentinel_environment_match"] = True

        for name, report in (
            ("history_created", history),
            ("query_in_cmdline", query_in_cmdline),
            ("query_in_environment", sentinel_environment),
        ):
            with self.subTest(name=name):
                self._assert_schema_valid_and_consumer_rejects(report)

    def test_artifact_binding_rejects_escaping_and_nested_paths(self):
        for filename in ("../outside.bin", "/tmp/outside.bin", "nested/output.bin"):
            with self.subTest(filename=filename):
                report = self._sdcv_report()
                report["cases"][0]["stdout_file"] = filename
                self._validate_schema(report)
                with self.assertRaises(review_bundle.BundleError):
                    self._validate(report)

    def test_artifact_binding_requires_every_regular_nonsymlink_sibling(self):
        artifacts = (
            ("exact_hit", "stdout_file"),
            ("exact_hit", "stderr_file"),
            ("sentinel_cmdline", "cmdline_file"),
        )
        for case_name, field in artifacts:
            for kind in ("missing", "symlink", "directory"):
                with self.subTest(case=case_name, field=field, kind=kind):
                    report = self._sdcv_report()
                    with tempfile.TemporaryDirectory() as directory:
                        root = pathlib.Path(directory)
                        report_path = self._write_report(root, report)
                        case = next(
                            item
                            for item in report["cases"]
                            if item["name"] == case_name
                        )
                        artifact = root / case[field]
                        artifact.unlink()
                        if kind == "symlink":
                            target = root / "unreferenced-regular.bin"
                            target.write_bytes(b"target")
                            artifact.symlink_to(target)
                        elif kind == "directory":
                            artifact.mkdir()
                        with self.assertRaises(review_bundle.BundleError):
                            review_bundle.validate_sdcv_gate_reports([report_path])

    def test_artifact_binding_matches_recorded_stdout_and_stderr_sizes(self):
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream):
                report = self._sdcv_report()
                with tempfile.TemporaryDirectory() as directory:
                    root = pathlib.Path(directory)
                    report_path = self._write_report(root, report)
                    case = report["cases"][0]
                    (root / case[f"{stream}_file"]).write_bytes(b"size-drift")
                    with self.assertRaises(review_bundle.BundleError):
                        review_bundle.validate_sdcv_gate_reports([report_path])

    def test_artifact_binding_rejects_path_swap_after_open(self):
        for filename in (
            "exact_hit.stdout.bin",
            "sentinel_cmdline.cmdline.bin",
        ):
            with self.subTest(filename=filename):
                report = self._sdcv_report()
                with tempfile.TemporaryDirectory() as directory:
                    root = pathlib.Path(directory)
                    report_path = self._write_report(root, report)
                    target = root / filename
                    real_open = os.open
                    swapped = False

                    def swap_path_after_open(path, flags, *args):
                        nonlocal swapped
                        descriptor = real_open(path, flags, *args)
                        if pathlib.Path(path) == target and not swapped:
                            swapped = True
                            contents = target.read_bytes()
                            target.unlink()
                            target.write_bytes(contents)
                        return descriptor

                    with mock.patch.object(
                        review_bundle.os, "open", side_effect=swap_path_after_open
                    ):
                        with self.assertRaisesRegex(
                            review_bundle.BundleError,
                            "artifact changed during validation",
                        ):
                            review_bundle.validate_sdcv_gate_reports([report_path])
                    self.assertTrue(swapped)

    def test_sentinel_cmdline_binding_is_nonempty_exact_and_private(self):
        mutations = (
            ("empty", b""),
            ("metadata_mismatch", b"/usr/bin/sdcv\0--wrong\0"),
        )
        for name, contents in mutations:
            with self.subTest(name=name):
                report = self._sdcv_report()
                with tempfile.TemporaryDirectory() as directory:
                    root = pathlib.Path(directory)
                    report_path = self._write_report(root, report)
                    sentinel = next(
                        case
                        for case in report["cases"]
                        if case["name"] == "sentinel_cmdline"
                    )
                    (root / sentinel["cmdline_file"]).write_bytes(contents)
                    with self.assertRaises(review_bundle.BundleError):
                        review_bundle.validate_sdcv_gate_reports([report_path])

        report = self._sdcv_report()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            report_path = self._write_report(root, report)
            sentinel = next(
                case
                for case in report["cases"]
                if case["name"] == "sentinel_cmdline"
            )
            (root / sentinel["cmdline_file"]).write_bytes(
                b"cmdline-" + b"a" * 48 + b"\0--json\0"
            )
            with self.assertRaisesRegex(
                review_bundle.BundleError,
                "sentinel query appeared in the cmdline capture",
            ):
                review_bundle.validate_sdcv_gate_reports([report_path])

    def test_sentinel_requires_expected_query_length_and_query_sent_decision(self):
        wrong_length = self._sdcv_report()
        sentinel = next(
            case for case in wrong_length["cases"] if case["name"] == "sentinel_cmdline"
        )
        sentinel["query_bytes"] = 0

        missing_query_sent = self._sdcv_report()
        sentinel = next(
            case
            for case in missing_query_sent["cases"]
            if case["name"] == "sentinel_cmdline"
        )
        sentinel["runner_decisions"] = []

        for name, report in (
            ("wrong_query_length", wrong_length),
            ("missing_query_sent", missing_query_sent),
        ):
            with self.subTest(name=name):
                self._validate_schema(report)
                with self.assertRaises(review_bundle.BundleError):
                    self._validate(report)

    def test_schema_valid_cap_termination_and_reaping_mismatches_are_rejected(self):
        mutations = (
            ("stdout_over_cap", "termination_reason", "timeout"),
            ("stdout_over_cap", "stdout_bytes", self.STDOUT_CAP_BYTES + 2),
            ("stdout_over_cap", "stdout_stored_bytes", self.STDOUT_CAP_BYTES - 1),
            ("stderr_over_cap", "termination_reason", "timeout"),
            ("stderr_over_cap", "stderr_bytes", self.STDERR_CAP_BYTES + 2),
            ("stderr_over_cap", "stderr_stored_bytes", self.STDERR_CAP_BYTES - 1),
            ("source_timeout", "termination_reason", "cancelled"),
            ("overall_timeout", "returncode", 0),
            ("cancellation", "process_group_reaped", False),
            ("exact_hit", "returncode", -9),
            ("stdout_under_cap_32mib", "stdout_bytes", 32 * 1024 * 1024),
        )
        for case_name, field, value in mutations:
            with self.subTest(case=case_name, field=field):
                report = self._sdcv_report()
                case = next(
                    item for item in report["cases"] if item["name"] == case_name
                )
                case[field] = value
                self._assert_schema_valid_and_consumer_rejects(report)

    def test_terminal_decisions_require_one_coherent_reason_and_sigkill(self):
        for case_name in review_bundle.SDCV_EXPECTED_TERMINATIONS:
            with self.subTest(case=case_name, mutation="wrong_signal"):
                report = self._sdcv_report()
                case = next(
                    item for item in report["cases"] if item["name"] == case_name
                )
                case["returncode"] = -signal.SIGTERM
                self._assert_schema_valid_and_consumer_rejects(report)

        conflicting = self._sdcv_report()
        timeout = next(
            case for case in conflicting["cases"] if case["name"] == "source_timeout"
        )
        timeout["runner_decisions"].append("cancelled")
        self._assert_schema_valid_and_consumer_rejects(conflicting)

        clean_with_terminal = self._sdcv_report()
        clean_with_terminal["cases"][0]["runner_decisions"] = ["timeout"]
        self._assert_schema_valid_and_consumer_rejects(clean_with_terminal)

        duplicate = self._sdcv_report()
        timeout = next(
            case for case in duplicate["cases"] if case["name"] == "source_timeout"
        )
        timeout["runner_decisions"] = ["timeout", "timeout"]
        with self.assertRaises(review_bundle.BundleError):
            review_bundle._validate_sdcv_gate_semantics(duplicate)

    def test_report_parser_rejects_all_nonstandard_nonfinite_json_numbers(self):
        for token in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(token=token):
                report = self._sdcv_report()
                encoded = json.dumps(report, ensure_ascii=False, allow_nan=False)
                raw = encoded.replace(
                    '"duration_ms": 1.0', f'"duration_ms": {token}', 1
                )
                self.assertNotEqual(raw, encoded)
                with tempfile.TemporaryDirectory() as directory:
                    path = self._write_report(
                        pathlib.Path(directory), report, raw_json=raw
                    )
                    with self.assertRaises(review_bundle.BundleError):
                        review_bundle.validate_sdcv_gate_reports([path])

    def test_report_semantics_require_finite_numeric_values(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                report = self._sdcv_report()
                report["cases"][0]["duration_ms"] = value
                with self.assertRaises(review_bundle.BundleError):
                    review_bundle._validate_sdcv_gate_semantics(report)

        report = self._sdcv_report()
        review_bundle._validate_sdcv_gate_semantics(report)

    def test_producer_report_serialization_forbids_nonfinite_numbers(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                report = self._sdcv_report()
                report["cases"][0]["duration_ms"] = value
                with self.assertRaises(ValueError):
                    probe_sdcv_stdin.serialize_report(report)

        serialized = probe_sdcv_stdin.serialize_report(self._sdcv_report())
        self.assertTrue(serialized.endswith("\n"))
        self.assertEqual(json.loads(serialized)["verdict"], "pass")

    def test_real_production_case_generation_schema_and_consumer_contract(self):
        child_source = textwrap.dedent(
            """\
            #!__PYTHON__
            import os
            import sys

            if "--version" in sys.argv:
                print("Console version of Stardict, version 0.5.5")
                raise SystemExit(0)

            prompt = "Enter word or phrase: "
            choice_prompt = "Your choice[-1 to abort]: "
            sys.stdout.write(prompt)
            sys.stdout.flush()

            import json
            import time

            query_line = sys.stdin.readline()
            if query_line == "":
                sys.stdout.write("\\n")
                sys.stdout.flush()
                raise SystemExit(0)

            query = query_line.rstrip("\\n")
            if query == "giant-over-cap":
                chunk = b"x" * (1024 * 1024)
                while True:
                    os.write(1, chunk)
            if query == "giant-under-cap":
                time.sleep(0.10)

            sys.stdout.write(query + "\\n")
            if "--exact-search" not in sys.argv:
                sys.stdout.write("[0)DANK_STDIN_A-->word\\n")
                sys.stdout.write(choice_prompt)
                sys.stdout.flush()
                choice = sys.stdin.readline().rstrip("\\n")
                entries = [
                    {
                        "dict": "DANK_STDIN_A",
                        "word": "word",
                        "definition": "fixture-a exact english",
                    }
                ]
                sys.stdout.write(choice + "\\n")
                sys.stdout.write(
                    json.dumps(
                        entries, ensure_ascii=False, separators=(",", ":")
                    )[1:]
                    + "\\n"
                )
            else:
                dictionaries = [
                    sys.argv[index + 1]
                    for index, value in enumerate(sys.argv[:-1])
                    if value == "--use-dict"
                ]
                if query == "definitely-absent" or query.startswith("cmdline-"):
                    entries = []
                elif query == "giant-under-cap":
                    tail = "GIANT-32-COMPLETE-TAIL"
                    definition = "x" * (32 * 1024 * 1024 - len(tail)) + tail
                    entries = [
                        {
                            "dict": "DANK_STDIN_GIANT32",
                            "word": query,
                            "definition": definition,
                        }
                    ]
                elif dictionaries == ["DANK_STDIN_B"]:
                    entries = [
                        {
                            "dict": "DANK_STDIN_B",
                            "word": query,
                            "definition": "fixture-b exact english",
                        }
                    ]
                elif dictionaries == ["DANK_STDIN_A", "DANK_STDIN_B"]:
                    entries = [
                        {
                            "dict": "DANK_STDIN_A",
                            "word": query,
                            "definition": "fixture-a exact english",
                        },
                        {
                            "dict": "DANK_STDIN_B",
                            "word": query,
                            "definition": "fixture-b exact english",
                        },
                    ]
                else:
                    definitions = {
                        "word": "fixture-a exact english",
                        "漢字": "fixture-a han unicode",
                        "かな": "fixture-a ja unicode",
                        "粵語": "fixture-a yue unicode",
                    }
                    entries = [
                        {
                            "dict": "DANK_STDIN_A",
                            "word": query,
                            "definition": definitions[query],
                        }
                    ]
                sys.stdout.write(
                    json.dumps(
                        entries, ensure_ascii=False, separators=(",", ":")
                    )
                    + "\\n"
                )
            sys.stdout.write(prompt + "\\n")
            sys.stdout.flush()
            sys.stdin.read()
            """
        ).replace("__PYTHON__", sys.executable)
        expected_case_fields = {
            "name",
            "argv_metadata",
            "query_bytes",
            "choice",
            "pid",
            "returncode",
            "duration_ms",
            "termination_reason",
            "stdout_bytes",
            "stdout_stored_bytes",
            "stderr_bytes",
            "stderr_stored_bytes",
            "stdout_cap_bytes",
            "stderr_cap_bytes",
            "stdout_file",
            "stderr_file",
            "cmdline_file",
            "query_in_cmdline",
            "main_prompt_count",
            "choice_prompt_count",
            "choice_aborted",
            "process_group_reaped",
            "runner_decisions",
            "errors",
            "status",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            evidence_dir = root / "bound-evidence"
            child = root / "hermetic-sdcv"
            child.write_text(child_source, encoding="utf-8")
            child.chmod(0o755)

            def empty_fixture_tree(fixture_root, *, include_giant=False):
                self.assertTrue(include_giant)
                data_root = fixture_root / "empty-stardict"
                data_root.mkdir()
                return data_root

            output = io.StringIO()
            with mock.patch.object(
                probe_sdcv_stdin,
                "build_fixture_tree",
                side_effect=empty_fixture_tree,
            ), mock.patch("sys.stdout", output):
                returncode = probe_sdcv_stdin.main(
                    [
                        "--sdcv",
                        str(child),
                        "--evidence-dir",
                        str(evidence_dir),
                    ]
                )
            self.assertEqual(returncode, 0, output.getvalue())
            self.assertIn("SDCV_STDIN_GATE: PASS", output.getvalue())

            report_path = evidence_dir / "report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            cases = report["cases"]
            self.assertEqual(
                [case["name"] for case in cases], list(self.CASE_NAMES)
            )
            for case in cases:
                self.assertEqual(
                    set(case) - {"parser", "sentinel_environment_match"},
                    expected_case_fields,
                )
                self.assertEqual(
                    case["stdout_file"], f"{case['name']}.stdout.bin"
                )
                self.assertEqual(
                    case["stderr_file"], f"{case['name']}.stderr.bin"
                )
                for stream in ("stdout", "stderr"):
                    artifact = evidence_dir / case[f"{stream}_file"]
                    self.assertTrue(artifact.is_file())
                    self.assertFalse(artifact.is_symlink())
                    self.assertEqual(
                        artifact.stat().st_size,
                        case[f"{stream}_stored_bytes"],
                    )
                expected_cmdline = (
                    "sentinel_cmdline.cmdline.bin"
                    if case["name"] == "sentinel_cmdline"
                    else None
                )
                self.assertEqual(case["cmdline_file"], expected_cmdline)
                self.assertEqual(case["errors"], [])
                self.assertEqual(case["status"], "pass")

            exact_hit = cases[0]
            sentinel = cases[-1]
            by_name = {case["name"]: case for case in cases}
            self.assertEqual(exact_hit["query_bytes"], 4)
            self.assertEqual(exact_hit["returncode"], 0)
            self.assertIsNone(exact_hit["termination_reason"])
            self.assertEqual(
                exact_hit["runner_decisions"], ["query_sent", "stdin_closed"]
            )
            self.assertEqual(sentinel["query_bytes"], 56)
            self.assertEqual(
                sentinel["cmdline_file"], "sentinel_cmdline.cmdline.bin"
            )
            self.assertEqual(sentinel["runner_decisions"].count("query_sent"), 1)
            self.assertIs(sentinel["query_in_cmdline"], False)
            self.assertIs(sentinel["sentinel_environment_match"], False)
            cmdline = (evidence_dir / sentinel["cmdline_file"]).read_bytes()
            self.assertTrue(cmdline)
            self.assertNotIn(b"cmdline-", cmdline)

            under_cap = by_name["stdout_under_cap_32mib"]
            self.assertGreater(under_cap["stdout_bytes"], 32 * 1024 * 1024)
            self.assertLess(under_cap["stdout_bytes"], self.STDOUT_CAP_BYTES)
            for name, reason, cap, stream in (
                ("stdout_over_cap", "stdout_cap", self.STDOUT_CAP_BYTES, "stdout"),
                ("stderr_over_cap", "stderr_cap", self.STDERR_CAP_BYTES, "stderr"),
            ):
                case = by_name[name]
                self.assertEqual(case["termination_reason"], reason)
                self.assertEqual(case["returncode"], -signal.SIGKILL)
                self.assertEqual(case[f"{stream}_bytes"], cap + 1)
                self.assertEqual(case[f"{stream}_stored_bytes"], cap)
            for name, reason in (
                ("source_timeout", "timeout"),
                ("overall_timeout", "timeout"),
                ("cancellation", "cancelled"),
            ):
                case = by_name[name]
                self.assertEqual(case["termination_reason"], reason)
                self.assertEqual(case["returncode"], -signal.SIGKILL)
            self._validate_schema(report)
            review_bundle.validate_sdcv_gate_reports([report_path])

    def test_schema_rejects_malformed_producer_reports(self):
        missing_raw = self._sdcv_report()
        del missing_raw["sdcv_version_raw"]
        wrong_cap = self._sdcv_report()
        wrong_cap["stderr_cap_bytes"] = self.STDERR_CAP_BYTES - 1
        wrong_case_cap = self._sdcv_report()
        wrong_case_cap["cases"][0]["stderr_cap_bytes"] = self.STDERR_CAP_BYTES - 1
        wrong_gate_errors = self._sdcv_report()
        wrong_gate_errors["gate_errors"] = "not-an-array"
        wrong_verdict = self._sdcv_report()
        wrong_verdict["verdict"] = "unknown"

        for name, report in (
            ("missing_raw_version", missing_raw),
            ("wrong_stderr_cap", wrong_cap),
            ("wrong_case_stderr_cap", wrong_case_cap),
            ("wrong_gate_errors_shape", wrong_gate_errors),
            ("wrong_verdict", wrong_verdict),
        ):
            with self.subTest(name=name), self.assertRaises(
                jsonschema.exceptions.ValidationError
            ):
                self._validate_schema(report)

    def test_consumer_rejects_schema_valid_semantic_mismatches(self):
        for name, field, value in (
            ("fail_verdict", "verdict", "fail"),
            ("nonempty_gate_errors", "gate_errors", ["forced failure"]),
            (
                "non_normalized_version",
                "sdcv_version",
                "Console version of Stardict, version 0.5.5",
            ),
            ("near_match_version", "sdcv_version", "0.5.50"),
        ):
            with self.subTest(name=name):
                report = self._sdcv_report()
                report[field] = value
                self._validate_schema(report)
                with self.assertRaises(review_bundle.BundleError):
                    self._validate(report)

    def test_review_bundle_content_validation_delegates_sdcv_group(self):
        paths = [pathlib.Path("sdcv-report.json")]
        with (
            mock.patch.object(
                review_bundle,
                "validate_sdcv_gate_reports",
                side_effect=review_bundle.BundleError("delegated sdcv rejection"),
            ) as validate,
            self.assertRaisesRegex(review_bundle.BundleError, "delegated sdcv rejection"),
        ):
            review_bundle.validate_evidence_content({"sdcv_gate_report": paths})
        validate.assert_called_once_with(paths)

    def test_schema_version_1_is_rejected(self):
        report = self._sdcv_report()
        report["schema_version"] = 1
        with self.assertRaises(review_bundle.BundleError):
            self._validate(report)

    def test_wrong_or_missing_kind_is_rejected(self):
        for kind in ("wrong", None):
            with self.subTest(kind=kind):
                report = self._sdcv_report()
                if kind is None:
                    del report["kind"]
                else:
                    report["kind"] = kind
                with self.assertRaises(review_bundle.BundleError):
                    self._validate(report)

    def test_missing_stderr_cap_bytes_is_rejected(self):
        report = self._sdcv_report()
        del report["stderr_cap_bytes"]
        with self.assertRaises(review_bundle.BundleError):
            self._validate(report)

    def test_incorrect_stderr_cap_bytes_is_rejected(self):
        report = self._sdcv_report()
        report["stderr_cap_bytes"] = self.STDERR_CAP_BYTES - 1
        with self.assertRaises(review_bundle.BundleError):
            self._validate(report)

    def test_nonexact_raw_version_is_rejected(self):
        for version in ("0.5.50", "0.5.5.1", "0.5.5-beta", "0.5.5+meta"):
            with self.subTest(version=version):
                report = self._sdcv_report()
                report["sdcv_version_raw"] = (
                    f"Console version of Stardict, version {version}"
                )
                with self.assertRaises(review_bundle.BundleError):
                    self._validate(report)

    def test_empty_cases_are_rejected(self):
        report = self._sdcv_report()
        report["cases"] = []
        with self.assertRaises(review_bundle.BundleError):
            self._validate(report)

    def test_missing_stderr_over_cap_case_is_rejected(self):
        report = self._sdcv_report()
        report["cases"] = [
            case for case in report["cases"] if case["name"] != "stderr_over_cap"
        ]
        with self.assertRaises(review_bundle.BundleError):
            self._validate(report)

    def test_duplicate_stderr_over_cap_cases_are_rejected(self):
        report = self._sdcv_report()
        over_cap = next(
            case for case in report["cases"] if case["name"] == "stderr_over_cap"
        )
        report["cases"].append(copy.deepcopy(over_cap))
        with self.assertRaises(review_bundle.BundleError):
            self._validate(report)

    def test_incorrect_stderr_over_cap_semantics_are_rejected(self):
        mutations = {
            "termination_reason": "timeout",
            "stderr_bytes": self.STDERR_CAP_BYTES + 2,
            "stderr_stored_bytes": self.STDERR_CAP_BYTES - 1,
            "process_group_reaped": False,
            "stderr_cap_bytes": self.STDERR_CAP_BYTES - 1,
            "status": "fail",
            "errors": ["stderr gate failed"],
        }
        for field, invalid_value in mutations.items():
            with self.subTest(field=field):
                report = self._sdcv_report()
                over_cap = next(
                    case
                    for case in report["cases"]
                    if case["name"] == "stderr_over_cap"
                )
                over_cap[field] = invalid_value
                with self.assertRaises(review_bundle.BundleError):
                    self._validate(report)


class PackagingMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="dank-lookup-packaging-")
        self.base = pathlib.Path(self.temporary.name)
        self.home = self.base / "home"
        self.config = self.base / "config"
        self.data = self.base / "data"
        self.home.mkdir()
        self.config.mkdir()
        self.data.mkdir()
        self.env = os.environ.copy()
        self.env.update(
            {
                "HOME": str(self.home),
                "XDG_CONFIG_HOME": str(self.config),
                "XDG_DATA_HOME": str(self.data),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @property
    def roots(self) -> dict[str, pathlib.Path]:
        return {
            "home_bin": self.home / ".local/bin",
            "app_lib": self.home / ".local/lib/dank-lookup",
            "quickshell": self.config / "quickshell/dank-lookup",
            "systemd_user": self.config / "systemd/user",
            "matugen": self.data / "dank-lookup/matugen",
            "dms": self.config / "DankMaterialShell/plugins/dankLookup",
        }

    @property
    def manifest_path(self) -> pathlib.Path:
        return self.roots["app_lib"] / "managed-files.json"

    def run_script(self, name: str, *arguments: str, expected: int = 0) -> subprocess.CompletedProcess[str]:
        run = subprocess.run(
            [str(PACKAGING / name), *arguments],
            cwd=ROOT,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(run.returncode, expected, run.stdout + run.stderr)
        return run

    def install(self, with_dms: bool = False) -> dict:
        arguments = ("--with-dms",) if with_dms else ()
        self.run_script("install-user.sh", *arguments)
        return managed_files.load_manifest(self.manifest_path)

    def assert_manifest_matches_regular_files(self, manifest: dict) -> None:
        for root_key, entries in manifest["roots"].items():
            for relative in entries:
                path = self.roots[root_key] / relative
                self.assertTrue(path.is_file(), f"missing managed file: {path}")
                self.assertFalse(path.is_symlink(), f"managed symlink: {path}")

    def test_fresh_install_and_uninstall_preserve_user_data(self):
        manifest = self.install()
        self.assert_manifest_matches_regular_files(manifest)
        self.assertEqual(manifest["roots"]["dms"], [])
        config_path = self.config / "dank-lookup/config.json"
        config_before = config_path.read_bytes()
        dictionary = self.data / "stardict/dic/user-dictionary.sentinel"
        dictionary.parent.mkdir(parents=True)
        dictionary.write_text("user dictionary", encoding="utf-8")
        unmanaged = self.roots["quickshell"] / "user-unmanaged.sentinel"
        unmanaged.write_text("keep", encoding="utf-8")

        self.run_script("uninstall-user.sh")
        self.assertEqual(config_path.read_bytes(), config_before)
        self.assertEqual(dictionary.read_text(encoding="utf-8"), "user dictionary")
        self.assertEqual(unmanaged.read_text(encoding="utf-8"), "keep")
        self.assertFalse(self.manifest_path.exists())
        for root_key, entries in manifest["roots"].items():
            for relative in entries:
                self.assertFalse((self.roots[root_key] / relative).exists())

    def _fake_tokenizer(self, name: str, *, speaks_stdin: bool) -> pathlib.Path:
        path = self.base / name
        if speaks_stdin:
            body = "#!/bin/sh\ncat >/dev/null\nprintf '{\"tokens\":[\"測\",\"試\"]}\\n'\n"
        else:
            body = "#!/bin/sh\nprintf 'unknown option: --stdin\\n' >&2\n"
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_tokenizer_bin_is_installed_managed_preserved_and_uninstalled(self):
        tokenizer = self._fake_tokenizer("new-tokenize", speaks_stdin=True)
        self.run_script("install-user.sh", "--tokenizer-bin", str(tokenizer))
        installed = self.roots["app_lib"] / "dank-lookup-tokenize"
        manifest = managed_files.load_manifest(self.manifest_path)
        self.assertIn("dank-lookup-tokenize", manifest["roots"]["app_lib"])
        self.assertEqual(installed.read_bytes(), tokenizer.read_bytes())
        # A plain reinstall keeps the managed tokenizer.
        run = self.run_script("install-user.sh")
        self.assertTrue(installed.is_file())
        self.assertNotIn("incompatible tokenizer", run.stderr)
        self.assertIn("dank-lookup-tokenize", managed_files.load_manifest(self.manifest_path)["roots"]["app_lib"])
        self.run_script("uninstall-user.sh")
        self.assertFalse(installed.exists())

    def test_unmanaged_old_tokenizer_is_kept_and_reported_loudly(self):
        old = self._fake_tokenizer("old-tokenize", speaks_stdin=False)
        installed = self.roots["app_lib"] / "dank-lookup-tokenize"
        installed.parent.mkdir(parents=True)
        installed.write_bytes(old.read_bytes())
        installed.chmod(0o755)
        run = self.run_script("install-user.sh")
        self.assertIn("incompatible tokenizer", run.stderr)
        self.assertEqual(installed.read_bytes(), old.read_bytes())
        self.assertNotIn("dank-lookup-tokenize", managed_files.load_manifest(self.manifest_path)["roots"]["app_lib"])
        new = self._fake_tokenizer("new-tokenize", speaks_stdin=True)
        refused = self.run_script("install-user.sh", "--tokenizer-bin", str(new), expected=1)
        self.assertIn("refusing to overwrite unmanaged tokenizer", refused.stderr)
        self.assertEqual(installed.read_bytes(), old.read_bytes())

    def test_tokenizer_bin_without_stdin_cli_is_rejected_before_install(self):
        old = self._fake_tokenizer("old-tokenize", speaks_stdin=False)
        run = self.run_script("install-user.sh", "--tokenizer-bin", str(old), expected=1)
        self.assertIn("does not answer the stdin CLI", run.stderr)
        self.assertFalse(self.manifest_path.exists())

    def test_manifest_upgrade_removes_old_new_and_preserves_dms_scope(self):
        manifest = self.install(with_dms=True)
        removed_qml = self.roots["quickshell"] / "qml/RemovedManaged.qml"
        removed_qml.write_text("old", encoding="utf-8")
        old_dms = self.roots["dms"] / "shared/OldManaged.qml"
        old_dms.write_text("old dms", encoding="utf-8")
        unmanaged_dms = self.roots["dms"] / "user-unmanaged.sentinel"
        unmanaged_dms.write_text("keep", encoding="utf-8")
        manifest["roots"]["quickshell"].append("qml/RemovedManaged.qml")
        manifest["roots"]["dms"].append("shared/OldManaged.qml")
        managed_files.atomic_write_manifest(
            self.manifest_path, managed_files.validate_manifest(manifest)
        )

        preserved = self.install(with_dms=False)
        self.assertFalse(removed_qml.exists())
        self.assertTrue(old_dms.is_file())
        self.assertIn("shared/OldManaged.qml", preserved["roots"]["dms"])
        self.assertEqual(unmanaged_dms.read_text(encoding="utf-8"), "keep")

        upgraded = self.install(with_dms=True)
        self.assertFalse(old_dms.exists())
        self.assertNotIn("shared/OldManaged.qml", upgraded["roots"]["dms"])
        self.assertEqual(unmanaged_dms.read_text(encoding="utf-8"), "keep")

    def test_marker_only_legacy_upgrade_cleans_only_declared_paths(self):
        app_lib = self.roots["app_lib"]
        quickshell = self.roots["quickshell"]
        (app_lib).mkdir(parents=True)
        (quickshell / "qml").mkdir(parents=True)
        (app_lib / ".installed-by-dank-lookup").write_text("", encoding="utf-8")
        (quickshell / ".installed-by-dank-lookup").write_text("", encoding="utf-8")
        legacy_lib = app_lib / "dank_lookup.py"
        legacy_qml = quickshell / "qml/Lookup.qml"
        legacy_lib.write_text("legacy", encoding="utf-8")
        legacy_qml.write_text("legacy", encoding="utf-8")
        unmanaged = quickshell / "qml/User.qml"
        unmanaged.write_text("keep", encoding="utf-8")

        manifest = self.install()
        self.assertFalse(legacy_lib.exists())
        self.assertFalse(legacy_qml.exists())
        self.assertEqual(unmanaged.read_text(encoding="utf-8"), "keep")
        self.assertNotIn("dank_lookup.py", manifest["roots"]["app_lib"])
        self.assertNotIn("qml/Lookup.qml", manifest["roots"]["quickshell"])

    def test_invalid_manifest_blocks_install_and_uninstall_without_mutation(self):
        self.manifest_path.parent.mkdir(parents=True)
        sentinel = self.manifest_path.parent / "sentinel"
        sentinel.write_text("keep", encoding="utf-8")
        self.manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "roots": {"app_lib": ["../sentinel"]},
                }
            ),
            encoding="utf-8",
        )
        self.run_script("install-user.sh", expected=1)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        self.run_script("uninstall-user.sh", expected=1)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
