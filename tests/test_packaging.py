#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tarfile
import tempfile
import unittest


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
