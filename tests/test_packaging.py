#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tarfile
import tempfile
import unittest

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

IDENTITY_SPEC = importlib.util.spec_from_file_location(
    "source_identity", ROOT / "tests/source_identity.py"
)
assert IDENTITY_SPEC is not None and IDENTITY_SPEC.loader is not None
source_identity = importlib.util.module_from_spec(IDENTITY_SPEC)
IDENTITY_SPEC.loader.exec_module(source_identity)

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


class ReleaseEvidenceTests(unittest.TestCase):
    """The release pipeline: commit binding, fixed commands, manual adoption."""

    REQUIREMENTS = {
        "schema_version": 2,
        "automated": {
            "unit": {"commands": [["python3", "-c", "print('UNIT ok')"]]},
            "bench": {
                "commands": [[
                    "python3", "-c",
                    "import sys; open(sys.argv[1], 'w').write('{\"ok\": true}')",
                    "{out}/bench.json",
                ]],
                "outputs": ["bench.json"],
            },
        },
        "manual": {"owner": {"patterns": ["tests/evidence/release/owner/*.json"]}},
        "decisions": {},
    }

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = pathlib.Path(temporary.name)
        self.root = self.base / "repo"
        self.root.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.invalid")
        self._git("config", "user.name", "test")
        self._write_requirements(self.REQUIREMENTS)
        (self.root / "README.md").write_text("product\n", encoding="utf-8")
        (self.root / "app.py").write_text("print('v1')\n", encoding="utf-8")
        self.commit("initial")
        self.context_path = self.root / "tests/evidence/release/context.json"
        self.config = self.base / "config.json"

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.root, check=True, stdout=subprocess.PIPE, text=True
        ).stdout.strip()

    def _write_requirements(self, value: dict) -> None:
        path = self.root / release_evidence.REQUIREMENTS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def commit(self, message: str) -> str:
        self._git("add", "-A", "--", ".", ":!tests/evidence")
        self._git("commit", "-q", "-m", message)
        return self._git("rev-parse", "HEAD")

    def start(self, context_path: pathlib.Path | None = None) -> dict:
        return release_evidence.create_context(self.root, context_path or self.context_path)

    def run_all(self, context_path: pathlib.Path | None = None) -> None:
        for group in ("unit", "bench"):
            self.assertTrue(
                release_evidence.run_group(
                    self.root, context_path or self.context_path, group, config=self.config
                )
            )

    def owner_file(
        self, name: str = "a.json", *, commit: str | None = None, uncommitted: bool = False,
        directory: str = "owner",
    ) -> pathlib.Path:
        path = self.root / "tests/evidence/release" / directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        source = {
            "origin": "installed",
            "commit": commit or self._git("rev-parse", "HEAD"),
            "uncommitted_changes": uncommitted,
        }
        path.write_text(json.dumps({"pass": True, "tested_source": source}) + "\n", encoding="utf-8")
        return path

    def record_owner(self, context_path: pathlib.Path | None = None, name: str = "a.json") -> pathlib.Path:
        path = self.owner_file(name)
        release_evidence.record_manual(self.root, context_path or self.context_path, "owner", [path])
        return path

    def build(self, context_path: pathlib.Path | None = None) -> pathlib.Path:
        output = self.base / "out/review.tar.gz"
        review_bundle.build_bundle(
            self.root, context_path or self.context_path, output, environment={"sdcv": None}
        )
        return output

    def test_context_binds_commit_and_tree(self):
        context = self.start()
        self.assertEqual(context["commit"], self._git("rev-parse", "HEAD"))
        self.assertEqual(context["tree"], self._git("rev-parse", "HEAD^{tree}"))

    def test_bundle_source_is_the_commit_not_the_working_copy(self):
        self.start()
        self.run_all()
        self.record_owner()
        (self.root / "AGENTS.md.bak-20261107").write_text("local notes\n", encoding="utf-8")
        (self.root / "app.py").write_text("print('uncommitted')\n", encoding="utf-8")
        output = self.build()
        with tarfile.open(output, "r:gz") as archive:
            names = set(archive.getnames())
            app = archive.extractfile("dank-lookup-review/source/app.py").read()
            checksums = archive.extractfile("dank-lookup-review/checksums.sha256").read().decode()
            evidence = {
                name: archive.extractfile(name).read()
                for name in names
                if name.startswith("dank-lookup-review/evidence/")
            }
        self.assertNotIn("dank-lookup-review/source/AGENTS.md.bak-20261107", names)
        self.assertEqual(app, b"print('v1')\n")
        self.assertIn("dank-lookup-review/binding.json", names)
        for name, data in evidence.items():
            relative = name.removeprefix("dank-lookup-review/")
            self.assertIn(f"{hashlib.sha256(data).hexdigest()}  {relative}", checksums)
        self.assertEqual(len(evidence), 4)

    def test_commands_come_from_the_commit_not_the_working_copy(self):
        self.start()
        replaced = copy.deepcopy(self.REQUIREMENTS)
        replaced["automated"]["unit"]["commands"] = [["true"]]
        self._write_requirements(replaced)
        release_evidence.run_group(self.root, self.context_path, "unit", config=self.config)
        context = release_evidence.load_context(self.context_path)
        log_path = next(self.root / path for path in context["evidence"] if path.endswith("unit.log"))
        log = log_path.read_text(encoding="utf-8")
        self.assertIn("UNIT ok", log)
        self.assertNotIn("$ true", log)

    def test_commands_run_in_a_clean_worktree(self):
        self._write_requirements({
            **self.REQUIREMENTS,
            "automated": {"unit": {"commands": [[
                "python3", "-c",
                "import os, sys; sys.exit(1 if os.path.exists('stray.py') else 0)",
            ]]}},
        })
        self.commit("probe for stray files")
        (self.root / "stray.py").write_text("untracked\n", encoding="utf-8")
        self.start()
        self.assertTrue(
            release_evidence.run_group(self.root, self.context_path, "unit", config=self.config)
        )
        self.assertEqual(self._git("worktree", "list").count("\n"), 0)

    def test_failing_command_is_logged_but_not_recorded(self):
        self._write_requirements({
            **self.REQUIREMENTS,
            "automated": {"unit": {"commands": [["python3", "-c", "raise SystemExit(3)"]]}},
        })
        self.commit("failing unit")
        context = self.start()
        self.assertFalse(
            release_evidence.run_group(self.root, self.context_path, "unit", config=self.config)
        )
        log = self.root / release_evidence.session_dir(context).as_posix() / "unit.log"
        self.assertIn("[exit 3]", log.read_text(encoding="utf-8"))
        self.assertTrue(log.read_text(encoding="utf-8").rstrip().endswith("RUN: FAIL"))
        self.assertEqual(release_evidence.load_context(self.context_path)["evidence"], {})

    def test_benchmark_logs_name_the_config_they_read(self):
        uses_config = copy.deepcopy(self.REQUIREMENTS)
        uses_config["automated"]["unit"]["commands"] = [["python3", "-c", "pass", "{config}"]]
        self._write_requirements(uses_config)
        self.commit("config user")
        self.config.write_text('{"modes": []}', encoding="utf-8")
        context = self.start()
        release_evidence.run_group(self.root, self.context_path, "unit", config=self.config)
        log = (self.root / release_evidence.session_dir(context).as_posix() / "unit.log").read_text()
        self.assertIn(f"sha256 {hashlib.sha256(self.config.read_bytes()).hexdigest()}", log)

    def test_schema_one_contexts_cannot_be_adopted(self):
        self.start()
        old = self.root / "tests/evidence/release/context-0.8.0.json"
        old.write_text('{"schema_version": 1, "kind": "dank_lookup_release_context"}', encoding="utf-8")
        with self.assertRaisesRegex(release_evidence.ReleaseEvidenceError, "take owner-run evidence again"):
            release_evidence.adopt_manual(self.root, self.context_path, old)

    def test_missing_output_fails_the_group(self):
        broken = copy.deepcopy(self.REQUIREMENTS)
        broken["automated"]["bench"]["commands"] = [["python3", "-c", "pass"]]
        self._write_requirements(broken)
        self.commit("bench without output")
        self.start()
        self.assertFalse(
            release_evidence.run_group(self.root, self.context_path, "bench", config=self.config)
        )

    def test_manual_record_checks_group_and_pattern(self):
        self.start()
        stray = self.root / "tests/evidence/release/elsewhere.json"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_text("{}", encoding="utf-8")
        with self.assertRaises(release_evidence.ReleaseEvidenceError):
            release_evidence.record_manual(self.root, self.context_path, "owner", [stray])
        with self.assertRaises(release_evidence.ReleaseEvidenceError):
            release_evidence.record_manual(self.root, self.context_path, "unit", [stray])

    def test_missing_groups_are_named(self):
        self.start()
        release_evidence.run_group(self.root, self.context_path, "unit", config=self.config)
        with self.assertRaisesRegex(review_bundle.BundleError, "bench, owner"):
            self.build()

    def test_changed_evidence_is_rejected(self):
        self.start()
        self.run_all()
        path = self.record_owner()
        path.write_text('{"pass": false}\n', encoding="utf-8")
        with self.assertRaisesRegex(review_bundle.BundleError, "changed"):
            self.build()

    def test_automated_evidence_does_not_survive_a_new_commit(self):
        self.start()
        self.run_all()
        self.record_owner()
        (self.root / "README.md").write_text("typo fixed\n", encoding="utf-8")
        self.commit("docs only")
        second = self.root / "tests/evidence/release/context-2.json"
        self.start(second)
        release_evidence.adopt_manual(self.root, second, self.context_path)
        with self.assertRaisesRegex(review_bundle.BundleError, "bench, unit"):
            self.build(second)
        self.run_all(second)
        self.build(second)

    def test_manual_evidence_is_adopted_only_across_documentation_changes(self):
        self.start()
        self.record_owner()
        (self.root / "app.py").write_text("print('v2')\n", encoding="utf-8")
        self.commit("code change")
        second = self.root / "tests/evidence/release/context-2.json"
        self.start(second)
        with self.assertRaisesRegex(release_evidence.ReleaseEvidenceError, "app.py"):
            release_evidence.adopt_manual(self.root, second, self.context_path)

    def test_bundle_output_must_be_outside_the_repository(self):
        self.start()
        self.run_all()
        self.record_owner()
        with self.assertRaisesRegex(review_bundle.BundleError, "outside"):
            review_bundle.build_bundle(
                self.root, self.context_path, self.root / "review.tar.gz", environment={}
            )

    def test_tool_version_change_requires_a_fresh_probe(self):
        decisions = {"sdcv_stdin": {"tool": "sdcv", "version": "0.5.5",
                                    "record": "docs/decisions/sdcv-stdin.md", "probe": "probe"}}
        review_bundle.check_decisions(
            {"sdcv": "Console version of Stardict, version 0.5.5"}, decisions
        )
        for found in ("Console version of Stardict, version 0.5.6", None):
            with self.subTest(found=found):
                with self.assertRaisesRegex(review_bundle.BundleError, "run probe"):
                    review_bundle.check_decisions({"sdcv": found}, decisions)

    def test_manual_evidence_must_name_the_commit_it_measured(self):
        self.start()
        bare = self.root / "tests/evidence/release/owner/old-0.8.0.json"
        bare.parent.mkdir(parents=True, exist_ok=True)
        bare.write_text('{"pass": true}\n', encoding="utf-8")
        with self.assertRaisesRegex(release_evidence.ReleaseEvidenceError, "does not name"):
            release_evidence.record_manual(self.root, self.context_path, "owner", [bare])
        dirty = self.owner_file("dirty.json", uncommitted=True)
        with self.assertRaisesRegex(release_evidence.ReleaseEvidenceError, "uncommitted"):
            release_evidence.record_manual(self.root, self.context_path, "owner", [dirty])

    def test_manual_evidence_of_an_older_install_is_rejected_after_code_changes(self):
        old = self._git("rev-parse", "HEAD")
        (self.root / "app.py").write_text("print('v2')\n", encoding="utf-8")
        self.commit("code change, not reinstalled")
        self.start()
        stale = self.owner_file("stale.json", commit=old)
        with self.assertRaisesRegex(release_evidence.ReleaseEvidenceError, "app.py"):
            release_evidence.record_manual(self.root, self.context_path, "owner", [stale])

    def test_manual_evidence_of_an_older_install_counts_across_documentation(self):
        old = self._git("rev-parse", "HEAD")
        (self.root / "README.md").write_text("typo fixed\n", encoding="utf-8")
        self.commit("docs only")
        self.start()
        self.run_all()
        path = self.owner_file("older.json", commit=old)
        release_evidence.record_manual(self.root, self.context_path, "owner", [path])
        self.build()

    def test_declared_dependencies_narrow_what_invalidates_manual_evidence(self):
        narrowed = copy.deepcopy(self.REQUIREMENTS)
        narrowed["manual"]["owner"]["depends_on"] = ["tools/probe.py"]
        self._write_requirements(narrowed)
        (self.root / "tools").mkdir()
        (self.root / "tools/probe.py").write_text("print('probe v1')\n", encoding="utf-8")
        measured = self.commit("probe")
        (self.root / "app.py").write_text("print('v2')\n", encoding="utf-8")
        self.commit("product change outside the probe")
        self.start()
        path = self.owner_file("narrow.json", commit=measured)
        release_evidence.record_manual(self.root, self.context_path, "owner", [path])
        (self.root / "tools/probe.py").write_text("print('probe v2')\n", encoding="utf-8")
        self.commit("probe change")
        second = self.root / "tests/evidence/release/context-2.json"
        self.start(second)
        with self.assertRaisesRegex(release_evidence.ReleaseEvidenceError, "tools/probe.py"):
            release_evidence.adopt_manual(self.root, second, self.context_path)

    def test_source_identity_reads_checkout_and_installed_copy(self):
        checkout = source_identity.checkout_source(self.root)
        self.assertEqual(checkout["commit"], self._git("rev-parse", "HEAD"))
        self.assertFalse(checkout["uncommitted_changes"])
        (self.root / "app.py").write_text("print('edited')\n", encoding="utf-8")
        self.assertTrue(source_identity.checkout_source(self.root)["uncommitted_changes"])
        self.assertIsNone(source_identity.checkout_source(self.base))
        installed = self.base / "installed-source.json"
        self.assertIsNone(source_identity.installed_source(installed))
        installed.write_text('{"commit": "abc", "uncommitted_changes": false}\n', encoding="utf-8")
        self.assertEqual(
            source_identity.installed_source(installed),
            {"origin": "installed", "commit": "abc", "uncommitted_changes": False},
        )

    def test_repository_requirements_cover_every_product_gate(self):
        requirements = json.loads(
            (ROOT / release_evidence.REQUIREMENTS_PATH).read_text(encoding="utf-8")
        )
        release_evidence._validate_requirements(requirements)
        groups = set(requirements["automated"]) | set(requirements["manual"])
        self.assertLessEqual(set(review_bundle.CONTENT_CHECKS), groups)
        self.assertEqual(requirements["decisions"]["sdcv_stdin"]["version"], "0.5.5")
        for decision in requirements["decisions"].values():
            self.assertTrue((ROOT / decision["record"]).is_file())
        for group in requirements["manual"].values():
            for pattern in group.get("depends_on", []):
                self.assertTrue((ROOT / pattern).is_file(), pattern)



class SdcvProbeReportTests(unittest.TestCase):
    """The sdcv stdin probe is a decision tool; these test its own report."""

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

    def test_producer_pass_report_matches_schema(self):
        report = self._sdcv_report()
        self.assertEqual(report["verdict"], "pass")
        self.assertEqual(report["sdcv_version"], "0.5.5")
        self.assertEqual(report["gate_errors"], [])
        self._validate_schema(report)

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
        self.run_script("install-user.sh", *arguments, "--without-tokenizer")
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
        installed = json.loads(
            (self.roots["app_lib"] / "installed-source.json").read_text(encoding="utf-8")
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, stdout=subprocess.PIPE, text=True
        ).stdout.strip()
        self.assertEqual(installed["commit"], head)
        self.assertIsInstance(installed["uncommitted_changes"], bool)
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

    def assert_nothing_installed(self) -> None:
        self.assertFalse(self.manifest_path.exists())
        self.assertFalse((self.roots["home_bin"] / "dank-lookup").exists())
        self.assertFalse(self.roots["quickshell"].exists())

    def test_unmanaged_old_tokenizer_fails_install_loudly_and_is_kept(self):
        old = self._fake_tokenizer("old-tokenize", speaks_stdin=False)
        installed = self.roots["app_lib"] / "dank-lookup-tokenize"
        installed.parent.mkdir(parents=True)
        installed.write_bytes(old.read_bytes())
        installed.chmod(0o755)
        for arguments in ((), ("--without-tokenizer",)):
            with self.subTest(arguments=arguments):
                run = self.run_script("install-user.sh", *arguments, expected=1)
                self.assertIn("incompatible tokenizer", run.stderr)
                self.assertIn("nothing was installed", run.stderr)
                for option in ("--with-tokenizer", "--tokenizer-bin PATH", "Move it aside"):
                    self.assertIn(option, run.stderr)
                self.assertEqual(installed.read_bytes(), old.read_bytes())
                self.assert_nothing_installed()
        new = self._fake_tokenizer("new-tokenize", speaks_stdin=True)
        for arguments in (("--tokenizer-bin", str(new)), ("--with-tokenizer",)):
            with self.subTest(arguments=arguments):
                refused = self.run_script("install-user.sh", *arguments, expected=1)
                self.assertIn("refusing to overwrite unmanaged tokenizer", refused.stderr)
                self.assertEqual(installed.read_bytes(), old.read_bytes())
                self.assert_nothing_installed()

    def test_missing_tokenizer_fails_install_unless_explicitly_omitted(self):
        run = self.run_script("install-user.sh", expected=1)
        self.assertIn("no tokenizer installed", run.stderr)
        for option in ("--with-tokenizer", "--tokenizer-bin PATH", "--without-tokenizer"):
            self.assertIn(option, run.stderr)
        self.assert_nothing_installed()
        run = self.run_script("install-user.sh", "--without-tokenizer")
        self.assertIn("bigram", run.stdout)
        self.assertFalse((self.roots["app_lib"] / "dank-lookup-tokenize").exists())
        # Reinstalling with a compatible unmanaged tokenizer in place keeps it.
        compatible = self._fake_tokenizer("compatible-tokenize", speaks_stdin=True)
        installed = self.roots["app_lib"] / "dank-lookup-tokenize"
        installed.write_bytes(compatible.read_bytes())
        installed.chmod(0o755)
        self.run_script("install-user.sh")
        self.assertEqual(installed.read_bytes(), compatible.read_bytes())

    def test_tokenizer_options_are_mutually_exclusive(self):
        tokenizer = self._fake_tokenizer("new-tokenize", speaks_stdin=True)
        for arguments in (
            ("--with-tokenizer", "--without-tokenizer"),
            ("--tokenizer-bin", str(tokenizer), "--without-tokenizer"),
            ("--with-tokenizer", "--tokenizer-bin", str(tokenizer)),
        ):
            with self.subTest(arguments=arguments):
                self.run_script("install-user.sh", *arguments, expected=2)
                self.assert_nothing_installed()

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
        self.run_script("install-user.sh", "--without-tokenizer", expected=1)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        self.run_script("uninstall-user.sh", expected=1)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
