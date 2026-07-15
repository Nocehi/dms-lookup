#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import io
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
sys.path.insert(0, str(PACKAGING))

import importlib.util


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
