#!/usr/bin/env python3
"""Regression tests for the marker-aware release dependency preflight."""

from __future__ import annotations

import importlib.util
import pathlib
import tempfile
import unittest
from importlib.metadata import PackageNotFoundError


ROOT = pathlib.Path(__file__).resolve().parent.parent
HELPER_PATH = ROOT / "packaging/check-python-dependencies.py"
SPEC = importlib.util.spec_from_file_location("check_python_dependencies", HELPER_PATH)
assert SPEC is not None and SPEC.loader is not None
check_python_dependencies = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_python_dependencies)


class DependencyPreflightTests(unittest.TestCase):
    def _check(
        self,
        requirements: str,
        *,
        marker_python: str,
        versions: dict[str, str],
        python_version: tuple[int, int] = (3, 11),
        reject_lookup: set[str] | None = None,
    ) -> list[str]:
        with tempfile.TemporaryDirectory(prefix="dank-dependency-preflight-") as raw:
            path = pathlib.Path(raw) / "requirements.txt"
            path.write_text(requirements, encoding="utf-8")

            def lookup(name: str) -> str:
                if reject_lookup is not None and name in reject_lookup:
                    self.fail(f"inactive marker unexpectedly queried {name}")
                if name not in versions:
                    raise PackageNotFoundError(name)
                return versions[name]

            return check_python_dependencies.check_requirements(
                path,
                marker_environment={
                    "python_version": marker_python,
                    "python_full_version": f"{marker_python}.0",
                },
                version_lookup=lookup,
                python_version=python_version,
            )

    def test_active_marker_requires_exact_installed_version(self):
        requirement = 'typing-extensions==4.16.0; python_version < "3.13"\n'
        self.assertEqual(
            self._check(
                requirement,
                marker_python="3.12",
                versions={"typing-extensions": "4.16.0"},
            ),
            [],
        )
        with self.subTest("missing"):
            problems = self._check(requirement, marker_python="3.12", versions={})
            self.assertEqual(problems, ["missing typing-extensions==4.16.0"])
        with self.subTest("mismatch"):
            problems = self._check(
                requirement,
                marker_python="3.12",
                versions={"typing-extensions": "4.15.0"},
            )
            self.assertEqual(
                problems,
                ["typing-extensions: expected 4.16.0, found 4.15.0"],
            )

    def test_inactive_marker_is_not_looked_up(self):
        self.assertEqual(
            self._check(
                'typing-extensions==4.16.0; python_version < "3.13"\n',
                marker_python="3.13",
                versions={},
                reject_lookup={"typing-extensions"},
            ),
            [],
        )

    def test_non_exact_requirement_is_rejected_even_when_marker_is_inactive(self):
        problems = self._check(
            'typing-extensions>=4.4.0; python_version < "3.13"\n',
            marker_python="3.14",
            versions={},
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("non-exact requirement", problems[0])

    def test_unconditional_exact_pin_still_rejects_mismatch(self):
        problems = self._check(
            "jsonschema==4.26.0\n",
            marker_python="3.14",
            versions={"jsonschema": "4.25.1"},
        )
        self.assertEqual(problems, ["jsonschema: expected 4.26.0, found 4.25.1"])

    def test_python_below_supported_floor_is_rejected(self):
        problems = self._check(
            "jsonschema==4.26.0\n",
            marker_python="3.10",
            versions={"jsonschema": "4.26.0"},
            python_version=(3, 10),
        )
        self.assertEqual(
            problems,
            ["unsupported Python 3.10; requires >= 3.11"],
        )

    def test_repository_lock_is_exact_for_active_and_inactive_markers(self):
        versions = {
            "attrs": "26.1.0",
            "jsonschema": "4.26.0",
            "jsonschema-specifications": "2025.9.1",
            "packaging": "26.2",
            "referencing": "0.37.0",
            "rpds-py": "2026.6.3",
            "typing-extensions": "4.16.0",
        }
        lock = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
        self.assertEqual(self._check(lock, marker_python="3.11", versions=versions), [])
        versions.pop("typing-extensions")
        self.assertEqual(
            self._check(
                lock,
                marker_python="3.14",
                versions=versions,
                reject_lookup={"typing-extensions"},
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
