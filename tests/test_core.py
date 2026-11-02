#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from dank_lookup_core import (  # noqa: E402
    MAX_INLINE_FULL_CHARS,
    SdcvClient,
    TRUNCATION_MARKER,
    _preview_limits,
    _segment,
    builtin_config,
    candidate_modes,
    clean_definition,
    detect_features,
    lookup,
    render_entries,
    resolve_definition_format,
    load_config,
    source_exact_only,
)
from dank_lookup_sdcv_stdin import (  # noqa: E402
    STDERR_CAP_BYTES,
    SdcvProtocolError,
    parse_empty_eof,
    parse_protocol_stdout,
    protocol_argv,
    run_bounded_protocol,
)


def write_shell(path: pathlib.Path, body: str) -> pathlib.Path:
    path.write_text("#!/bin/sh\nset -eu\n" + textwrap.dedent(body).lstrip(), encoding="utf-8")
    path.chmod(0o755)
    return path


class FakeClient:
    available = True

    def __init__(self, installed=(), responses=None):
        self.installed = set(installed)
        self.responses = responses or {}
        self.backend_ns = 1_000_000
        self.calls = []
        self.candidate_policies = []

    def source_installed(self, source):
        return source["name"] in self.installed

    def query(self, source, query, *, candidates="all"):
        self.calls.append((source["name"], query))
        self.candidate_policies.append((source["name"], candidates))
        return list(self.responses.get((source["name"], query), [])), None


class MissingClient(FakeClient):
    available = False


class TokenBudgetClient(FakeClient):
    budget_exhausted = False

    def query(self, source, query, *, candidates="all"):
        self.calls.append((source["name"], query))
        if len(query) < 3:
            self.budget_exhausted = True
            return [], "lookup_budget_exhausted"
        return [], None


def config_for_tests():
    return {
        "version": 2,
        "preview": {"max_entry_chars": 64, "max_body_chars": 180},
        "modes": {
            "han": {
                "label": "Han",
                "default_for": ["han"],
                "sources": [{"name": "HanDict", "when": "han", "allow_related": False}],
            },
            "en": {
                "label": "English",
                "default_for": ["latin"],
                "sources": [{"name": "EnDict", "when": "latin", "allow_related": True}],
            },
            "ja": {
                "label": "Japanese",
                "default_for": ["kana", "han"],
                "sources": [{"name": "JaDict", "when": ["kana", "han"], "allow_related": False}],
            },
            "yue": {
                "label": "Cantonese",
                "default_for": ["han"],
                "sources": [{"name": "YueDict", "when": "han", "allow_related": False}],
            },
        },
        "fallback": {"mock": False, "unfiltered": False},
    }


class DetectionRoutingTests(unittest.TestCase):
    def test_builtin_config_has_exact_fresh_mode_ids(self):
        self.assertEqual(list(builtin_config()["modes"]), ["han", "en", "ja", "yue"])

    def test_fresh_configs_declare_boolean_exact_only_policy(self):
        fresh_configs = [
            builtin_config(),
            json.loads(
                (ROOT / "language-packs/default-config.json").read_text(encoding="utf-8")
            ),
        ]
        for config in fresh_configs:
            sources = [
                source
                for mode in config["modes"].values()
                for source in mode.get("sources", [])
            ]
            self.assertTrue(sources)
            self.assertTrue(all(isinstance(source.get("exact_only"), bool) for source in sources))

    def test_mixed_script_character_order_does_not_change_candidates(self):
        cfg = config_for_tests()
        first = detect_features("USB接口")
        second = detect_features("接口USB")
        self.assertEqual(first, second)
        self.assertEqual(candidate_modes(cfg, first), candidate_modes(cfg, second))
        self.assertEqual(candidate_modes(cfg, first), ["han", "en", "ja", "yue"])

    def test_kana_boosts_ja_without_hiding_other_exact_modes(self):
        query = "猫かな"
        responses = {
            (name, query): [{"dict": name, "word": query, "definition": f"{name} exact"}]
            for name in ("HanDict", "JaDict", "YueDict")
        }
        result, _ = self._lookup_with(config_for_tests(), FakeClient(responses=responses, installed={"HanDict", "JaDict", "YueDict"}), query)
        self.assertEqual(result["default_mode"], "ja")
        self.assertEqual(result["available_modes"], ["han", "ja", "yue"])

    def test_cantonese_marker_boosts_yue_without_hiding_other_exact_modes(self):
        query = "唔好"
        responses = {
            (name, query): [{"dict": name, "word": query, "definition": f"{name} exact"}]
            for name in ("HanDict", "JaDict", "YueDict")
        }
        result, _ = self._lookup_with(config_for_tests(), FakeClient(responses=responses, installed={"HanDict", "JaDict", "YueDict"}), query)
        self.assertEqual(result["default_mode"], "yue")
        self.assertEqual(result["available_modes"], ["han", "ja", "yue"])

    def test_mode_installation_status_is_independent_of_current_script(self):
        client = FakeClient(installed={"HanDict", "EnDict", "JaDict"})
        result, _ = self._lookup_with(config_for_tests(), client, "word")
        self.assertEqual(result["candidate_modes"], ["en"])
        self.assertEqual(result["installed_modes"], ["han", "en", "ja"])
        self.assertEqual(result["unavailable_modes"], ["yue"])

    def test_exact_and_related_are_strictly_separate(self):
        query = "word"
        client = FakeClient(
            installed={"EnDict"},
            responses={
                ("EnDict", query): [
                    {"dict": "EnDict", "word": "word", "definition": "exact"},
                    {"dict": "EnDict", "word": "words", "definition": "related"},
                ]
            },
        )
        result, _ = self._lookup_with(config_for_tests(), client, query)
        self.assertIn("exact", result["results_by_mode"]["en"]["body"])
        self.assertNotIn("related", result["results_by_mode"]["en"]["body"])
        self.assertIn("related", result["related_by_mode"]["en"]["body"])
        self.assertNotIn("exact", result["related_by_mode"]["en"]["body"])

    def test_exact_only_true_rejects_non_exact_headwords(self):
        cfg = config_for_tests()
        cfg["modes"]["en"]["sources"][0]["exact_only"] = True
        query = "word"
        client = FakeClient(
            installed={"EnDict"},
            responses={
                ("EnDict", query): [
                    {"dict": "EnDict", "word": "word", "definition": "exact"},
                    {"dict": "EnDict", "word": "words", "definition": "related"},
                ]
            },
        )
        result, _ = self._lookup_with(cfg, client, query)
        self.assertIn("exact", result["results_by_mode"]["en"]["body"])
        self.assertNotIn("en", result["related_by_mode"])

    def test_exact_only_true_accepts_nfkc_casefold_equal_headword(self):
        cfg = config_for_tests()
        cfg["modes"]["en"]["sources"][0]["exact_only"] = True
        query = "STRASSE"
        client = FakeClient(
            installed={"EnDict"},
            responses={
                ("EnDict", query): [
                    {"dict": "EnDict", "word": "Straße", "definition": "casefold exact"}
                ]
            },
        )
        result, _ = self._lookup_with(cfg, client, query)
        self.assertIn("casefold exact", result["results_by_mode"]["en"]["body"])
        self.assertNotIn("en", result["related_by_mode"])

    def test_exact_only_false_retains_bounded_related_candidates(self):
        cfg = config_for_tests()
        cfg["modes"]["en"]["sources"][0].update(
            {"exact_only": False, "allow_related": False}
        )
        query = "word"
        client = FakeClient(
            installed={"EnDict"},
            responses={
                ("EnDict", query): [
                    {"dict": "EnDict", "word": "words", "definition": "related"}
                ]
            },
        )
        result, _ = self._lookup_with(cfg, client, query)
        self.assertEqual(result["results_by_mode"], {})
        self.assertIn("related", result["related_by_mode"]["en"]["body"])

    def test_exact_only_no_match_and_multi_dictionary_policy(self):
        cfg = config_for_tests()
        cfg["modes"]["en"]["sources"] = [
            {"name": "Strict", "when": "latin", "exact_only": True},
            {"name": "Related", "when": "latin", "exact_only": False},
        ]
        query = "word"
        client = FakeClient(
            installed={"Strict", "Related"},
            responses={
                ("Strict", query): [
                    {"dict": "Strict", "word": "unrelated", "definition": "must drop"}
                ],
                ("Related", query): [
                    {"dict": "Related", "word": "words", "definition": "keep related"}
                ],
            },
        )
        result, _ = self._lookup_with(cfg, client, query)
        self.assertEqual(result["results_by_mode"], {})
        related = result["related_by_mode"]["en"]["body"]
        self.assertIn("keep related", related)
        self.assertNotIn("must drop", related)
        self.assertTrue(source_exact_only({"exact_only": True, "allow_related": True}))
        self.assertFalse(source_exact_only({"exact_only": False, "allow_related": False}))

    def test_missing_sdcv_is_structured_error(self):
        result, vault = lookup("word", client=MissingClient())
        self.assertEqual(result["backend"], "unavailable")
        self.assertEqual(result["error"]["code"], "sdcv_missing")
        self.assertEqual(vault, {"entries": {}, "full_blobs": {}})

    def test_fresh_home_builtin_modes_are_unavailable_without_dictionaries(self):
        with tempfile.TemporaryDirectory() as directory:
            missing_config = pathlib.Path(directory) / "fresh-home/config.json"
            result, _ = lookup("word", config_path=missing_config, client=FakeClient(installed=set()))
        self.assertFalse(result["config"]["loaded"])
        self.assertEqual(result["config"]["warning"], "config_not_found_using_builtin")
        self.assertEqual(result["installed_modes"], [])
        self.assertEqual(result["unavailable_modes"], ["han", "en", "ja", "yue"])
        self.assertEqual(result["available_modes"], [])

    def test_fresh_no_tokenizer_uses_honest_indexed_fallback_segmentation(self):
        query = "為甚麼會這樣"
        client = FakeClient(
            installed={"HanDict"},
            responses={("HanDict", "甚麼"): [{"dict": "HanDict", "word": "甚麼", "definition": "what"}]},
        )
        with mock.patch.dict(os.environ, {"DANK_LOOKUP_TOKENIZER": "/definitely/missing"}, clear=False):
            result, _ = self._lookup_with(config_for_tests(), client, query)
        self.assertTrue(result["segmented"])
        self.assertEqual(result["segmentation"]["engine"], "fallback-script-ngram")
        self.assertEqual(result["segmentation"]["warning"], "fallback_segmentation_nonlinguistic")
        item = next(item for item in result["token_items"] if item["token"] == "甚麼")
        self.assertIn(item["key"], result["results_by_token"])

    def test_pre_stdin_tokenizer_is_reported_as_incompatible(self):
        # The v0.7.1 tokenizer only took positional text; it answers the
        # stdin CLI with a usage error and no tokens.
        with tempfile.TemporaryDirectory() as directory:
            tokenizer = write_shell(
                pathlib.Path(directory) / "old-tokenize",
                """
                printf 'unknown option: --stdin\\n' >&2
                exit 0
                """,
            )
            client = FakeClient(
                installed={"HanDict"},
                responses={("HanDict", "甚麼"): [{"dict": "HanDict", "word": "甚麼", "definition": "what"}]},
            )
            with mock.patch.dict(os.environ, {"DANK_LOOKUP_TOKENIZER": str(tokenizer)}, clear=False):
                result, _ = self._lookup_with(config_for_tests(), client, "為甚麼會這樣")
        self.assertTrue(result["segmented"])
        self.assertEqual(result["segmentation"]["engine"], "fallback-script-ngram")
        self.assertEqual(result["segmentation"]["warning"], "tokenizer_incompatible")

    def test_fallback_segmentation_preserves_duplicate_tokens_with_indexed_keys_and_caps_count(self):
        query = "人" * 100
        client = FakeClient(
            installed={"HanDict"},
            responses={("HanDict", "人人"): [{"dict": "HanDict", "word": "人人", "definition": "people"}]},
        )
        with mock.patch.dict(os.environ, {"DANK_LOOKUP_TOKENIZER": "/definitely/missing"}, clear=False):
            result, _ = self._lookup_with(config_for_tests(), client, query)
        self.assertEqual(len(result["tokens"]), 24)
        self.assertEqual(result["tokens"][0], result["tokens"][1])
        self.assertEqual(result["token_items"][0]["key"], "0:人人")
        self.assertEqual(result["token_items"][1]["key"], "1:人人")

    def test_core_rejects_over_limit_query_before_backend_calls(self):
        client = FakeClient(installed={"EnDict"})
        result, _ = self._lookup_with(config_for_tests(), client, "a" * 5000)
        self.assertEqual(result["error"]["code"], "query_too_long")
        self.assertTrue(result["query_truncated"])
        self.assertEqual(len(result["query"]), 4096)
        self.assertEqual(client.calls, [])

    def test_token_lookup_budget_warning_promotes_to_top_level_timeout(self):
        client = TokenBudgetClient(installed={"HanDict"})
        with mock.patch.dict(os.environ, {"DANK_LOOKUP_TOKENIZER": "/definitely/missing"}, clear=False):
            result, _ = self._lookup_with(config_for_tests(), client, "人人人")
        self.assertFalse(result["segmented"])
        self.assertIn("lookup_budget_exhausted", result["warnings"])
        self.assertEqual(result["error"]["code"], "lookup_timeout")

    def _lookup_with(self, cfg, client, query):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "config.json"
            path.write_text(json.dumps(cfg), encoding="utf-8")
            return lookup(query, config_path=path, request_id="test", sequence=7, client=client)


class ProductionTimeoutBoundaryTests(unittest.TestCase):
    @staticmethod
    def _config(path: pathlib.Path, sources: list[str], source_ms: int, overall_ms: int) -> pathlib.Path:
        path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "source_timeout_ms": source_ms,
                    "lookup_timeout_ms": overall_ms,
                    "modes": {
                        "en": {
                            "label": "English",
                            "default_for": ["latin"],
                            "sources": [
                                {"name": name, "when": "latin", "allow_related": False}
                                for name in sources
                            ],
                        }
                    },
                    "fallback": {"mock": False, "unfiltered": False},
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_source_timeout_kills_and_reaps_direct_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            pid_file = root / "pid"
            fake = write_shell(
                root / "slow-sdcv",
                """
                for argument do
                    if [ "$argument" = "--list-dicts" ]; then
                        printf "%s\\n" "Dictionary's name   Word count" "SlowDict    1"
                        exit 0
                    fi
                done
                printf '%s\\n' "$$" > "$FAKE_PID_FILE"
                exec sleep 5
                """,
            )
            config = self._config(root / "config.json", ["SlowDict"], 100, 1000)
            client = SdcvClient(executable=str(fake), timeout=0.1)
            started = time.monotonic()
            with mock.patch.dict(os.environ, {"FAKE_PID_FILE": str(pid_file)}, clear=False):
                client.environment["FAKE_PID_FILE"] = str(pid_file)
                result, _vault = lookup("word", config_path=config, client=client)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.8)
            self.assertIn("sdcv_source_timeout", result["warnings"])
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)

    def test_overall_deadline_caps_multiple_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            call_log = root / "calls"
            fake = write_shell(
                root / "bounded-sdcv",
                """
                for argument do
                    if [ "$argument" = "--list-dicts" ]; then
                        printf "%s\\n" "Dictionary's name   Word count" "FirstDict    1" "SecondDict    1"
                        exit 0
                    fi
                done
                printf '%s\\n' "$*" >> "$FAKE_CALL_LOG"
                exec sleep 0.18
                """,
            )
            config = self._config(
                root / "config.json", ["FirstDict", "SecondDict"], 200, 250
            )
            client = SdcvClient(executable=str(fake), timeout=0.2)
            client.environment["FAKE_CALL_LOG"] = str(call_log)
            started = time.monotonic()
            result, _vault = lookup("word", config_path=config, client=client)
            elapsed = time.monotonic() - started
            self.assertGreaterEqual(elapsed, 0.15)
            self.assertLess(elapsed, 0.8)
            self.assertEqual(result["error"]["code"], "lookup_timeout")
            self.assertIn("lookup_budget_exhausted", result["warnings"])
            self.assertEqual(result["incomplete_modes"], ["en"])
            self.assertEqual(len(call_log.read_text(encoding="utf-8").splitlines()), 2)

    def test_result_returned_after_overall_deadline_is_rejected(self):
        now = 1_000_000_000
        observed_timeout: list[float] = []

        def clock_ns() -> int:
            return now

        def late_runner(argv, **kwargs):
            nonlocal now
            observed_timeout.append(kwargs["timeout"])
            now += 200_000_000
            return subprocess.CompletedProcess(
                argv,
                0,
                '[{"dict":"LateDict","word":"word","definition":"late"}]\n',
                "",
            )

        client = SdcvClient(
            executable="/bin/true",
            timeout=1.0,
            runner=late_runner,
            clock_ns=clock_ns,
        )
        client.deadline_ns = now + 100_000_000
        result = client._run(["/bin/true"])
        self.assertIsNone(result)
        self.assertTrue(client.budget_exhausted)
        self.assertAlmostEqual(observed_timeout[0], 0.1)


class SdcvBoundedProtocolTests(unittest.TestCase):
    def test_sustained_stderr_cap_kills_and_reaps_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            parent_pid_file = root / "parent.pid"
            child_pid_file = root / "child.pid"
            producer = root / "stderr-producer"
            producer.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/python3
                    import os
                    import pathlib
                    import time

                    pathlib.Path(os.environ["PARENT_PID_FILE"]).write_text(
                        str(os.getpid()), encoding="ascii"
                    )
                    child = os.fork()
                    if child == 0:
                        pathlib.Path(os.environ["CHILD_PID_FILE"]).write_text(
                            str(os.getpid()), encoding="ascii"
                        )
                        chunk = b"x" * (64 * 1024)
                        while True:
                            os.write(2, chunk)
                    while True:
                        time.sleep(1)
                    """
                ),
                encoding="utf-8",
            )
            producer.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "PARENT_PID_FILE": str(parent_pid_file),
                    "CHILD_PID_FILE": str(child_pid_file),
                }
            )

            run, _stdout = run_bounded_protocol(
                name="sustained-stderr",
                argv=[str(producer)],
                environment=environment,
                query="word",
                timeout_seconds=0.25,
            )

            self.assertEqual(run["termination_reason"], "stderr_cap")
            self.assertEqual(run["returncode"], -signal.SIGKILL)
            self.assertEqual(
                [
                    decision
                    for decision in run["runner_decisions"]
                    if decision
                    in {"timeout", "cancelled", "stdout_cap", "stderr_cap", "wait_timeout"}
                ],
                ["stderr_cap"],
            )
            self.assertEqual(run["stderr_stored_bytes"], STDERR_CAP_BYTES)
            self.assertGreater(run["stderr_bytes"], STDERR_CAP_BYTES)
            self.assertLessEqual(run["stderr_bytes"], STDERR_CAP_BYTES + 1)
            self.assertTrue(run["process_group_reaped"])
            for pid_file in (parent_pid_file, child_pid_file):
                pid = int(pid_file.read_text(encoding="ascii"))
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)


class StdinAdapterTests(unittest.TestCase):
    def test_stderr_cap_is_reported_distinctly_and_stops_followup_queries(self):
        calls = []

        def protocol_runner(**kwargs):
            calls.append(kwargs)
            return (
                {
                    "returncode": -9,
                    "termination_reason": "stderr_cap",
                    "stdout_bytes": 0,
                    "stderr_bytes": STDERR_CAP_BYTES + 1,
                    "choice_prompt_count": 0,
                    "process_group_reaped": True,
                },
                b"",
            )

        client = SdcvClient(
            executable="/bin/true",
            protocol_runner=protocol_runner,
        )
        entries, warning = client.query(
            {"name": "FixtureDict", "allow_related": False},
            "word",
        )
        self.assertEqual(entries, [])
        self.assertEqual(warning, "sdcv_stderr_cap")
        self.assertEqual(len(calls), 1)

    def test_sdcv_query_is_a_protocol_payload_not_an_argv_or_environment_value(self):
        calls = []

        def protocol_runner(**kwargs):
            calls.append(kwargs)
            query = kwargs["query"]
            payload = json.dumps(
                [{"dict": "FixtureDict", "word": query, "definition": "exact"}],
                ensure_ascii=False,
            ).encode("utf-8")
            stdout = b"Enter word or phrase: " + query.encode("utf-8") + b"\n" + payload + b"\nEnter word or phrase: \n"
            return (
                {
                    "returncode": 0,
                    "termination_reason": None,
                    "stdout_bytes": len(stdout),
                    "choice_prompt_count": 0,
                    "process_group_reaped": True,
                },
                stdout,
            )

        query = "private $(query) 日本語"
        client = SdcvClient(
            executable="/bin/true",
            protocol_runner=protocol_runner,
        )
        entries, warning = client.query(
            {"name": "FixtureDict", "allow_related": False},
            query,
        )
        self.assertIsNone(warning)
        self.assertEqual(entries[0]["word"], query)
        self.assertEqual(calls[0]["query"], query)
        self.assertNotIn(query, calls[0]["argv"])
        self.assertNotIn(query, calls[0]["environment"].values())
        self.assertEqual(calls[0]["environment"]["SDCV_HISTFILE"], "/dev/null")
        self.assertEqual(calls[0]["environment"]["SDCV_HISTSIZE"], "0")

    def test_related_candidate_protocol_uses_the_gate_proven_choice_frame(self):
        choices = []

        def protocol_runner(**kwargs):
            query = kwargs["query"]
            choices.append(kwargs["choice"])
            if query.startswith("/"):
                stdout = (
                    b"Enter word or phrase: /wrod\n"
                    b"[0)FixtureDict-->word\n"
                    b"Your choice[-1 to abort]: 0\n"
                    b'{"dict":"FixtureDict","word":"word","definition":"exact"}]\n'
                    b"Enter word or phrase: \n"
                )
                prompt_count = 1
            elif query == "word":
                stdout = (
                    b"Enter word or phrase: word\n"
                    b'[{"dict":"FixtureDict","word":"word","definition":"exact"}]\n'
                    b"Enter word or phrase: \n"
                )
                prompt_count = 0
            else:
                stdout = (
                    b"Enter word or phrase: wrod\n[]\n"
                    b"Enter word or phrase: \n"
                )
                prompt_count = 0
            return (
                {
                    "returncode": 0,
                    "termination_reason": None,
                    "stdout_bytes": len(stdout),
                    "choice_prompt_count": prompt_count,
                    "process_group_reaped": True,
                },
                stdout,
            )

        client = SdcvClient(executable="/bin/true", protocol_runner=protocol_runner)
        entries, warning = client.query(
            {"name": "FixtureDict", "allow_related": False},
            "wrod",
        )
        self.assertIsNone(warning)
        self.assertEqual([entry["word"] for entry in entries], ["word"])
        self.assertEqual(choices, [None, 0, None])

    @staticmethod
    def _headword_protocol_runner(headwords, calls):
        """Fake sdcv: exact search hits `headwords`; fuzzy lists all of them."""

        def protocol_runner(**kwargs):
            query = kwargs["query"]
            calls.append((query, kwargs["choice"]))
            head = f"Enter word or phrase: {query}\n".encode("utf-8")
            tail = b"\nEnter word or phrase: \n"
            if query.startswith("/"):
                options = "".join(
                    f"{index})FixtureDict-->{word}\n" for index, word in enumerate(headwords)
                )
                first = json.dumps(
                    {"dict": "FixtureDict", "word": headwords[0], "definition": "first"},
                    ensure_ascii=False,
                )
                stdout = head + f"[{options}Your choice[-1 to abort]: 0\n{first}]".encode("utf-8") + tail
                prompt_count = 1
            else:
                found = [
                    {"dict": "FixtureDict", "word": word, "definition": f"def {word}"}
                    for word in headwords
                    if word == query
                ]
                stdout = head + json.dumps(found, ensure_ascii=False).encode("utf-8") + tail
                prompt_count = 0
            return (
                {
                    "returncode": 0,
                    "termination_reason": None,
                    "stdout_bytes": len(stdout),
                    "choice_prompt_count": prompt_count,
                    "process_group_reaped": True,
                },
                stdout,
            )

        return protocol_runner

    def test_exact_only_uncased_query_runs_one_sdcv_process(self):
        # A CJK query has no case/NFKC variants, so an exact-only source needs
        # only its exact search; fuzzy candidates would all be discarded.
        calls = []
        runner = self._headword_protocol_runner(["春", "春天", "春節", "青春"], calls)
        client = SdcvClient(executable="/bin/true", protocol_runner=runner)
        entries, warning = client.query({"name": "FixtureDict"}, "春", candidates="variants")
        self.assertIsNone(warning)
        self.assertEqual([entry["word"] for entry in entries], ["春"])
        self.assertEqual(calls, [("春", None)])

    def test_exact_only_cased_query_fetches_only_casefold_variants(self):
        calls = []
        runner = self._headword_protocol_runner(["Linux", "Linum", "lignum", "LINUX"], calls)
        client = SdcvClient(executable="/bin/true", protocol_runner=runner)
        entries, warning = client.query({"name": "FixtureDict"}, "linux", candidates="variants")
        self.assertIsNone(warning)
        self.assertEqual(sorted(entry["word"] for entry in entries), ["LINUX", "Linux"])
        fetched = [query for query, choice in calls if not query.startswith("/")]
        self.assertEqual(fetched, ["linux", "Linux", "LINUX"])

    def test_fallback_candidates_fetch_everything_only_without_an_exact_entry(self):
        calls = []
        runner = self._headword_protocol_runner(["Linum", "Cinura", "jinx"], calls)
        client = SdcvClient(executable="/bin/true", protocol_runner=runner)
        entries, _warning = client.query({"name": "FixtureDict"}, "Linux", candidates="fallback")
        self.assertEqual([entry["word"] for entry in entries], ["Linum", "Cinura", "jinx"])

        calls.clear()
        runner = self._headword_protocol_runner(["Linux", "Linum", "Cinura"], calls)
        client = SdcvClient(executable="/bin/true", protocol_runner=runner)
        entries, _warning = client.query({"name": "FixtureDict"}, "Linux", candidates="fallback")
        self.assertEqual([entry["word"] for entry in entries], ["Linux"])
        self.assertEqual([query for query, _choice in calls], ["Linux", "/Linux"])

    def test_lookup_passes_candidate_policy_from_source_policy(self):
        config = {
            "version": 2,
            "modes": {
                "en": {
                    "sources": [
                        {"name": "Exact", "when": "any", "exact_only": True},
                        {"name": "Fuzzy", "when": "any", "exact_only": False},
                    ]
                }
            },
        }
        client = FakeClient(installed=["Exact", "Fuzzy"])
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            lookup("word", config_path=path, client=client)
            v1 = {"version": 1, "modes": {"en": {"sources": [
                {"name": "Exact", "when": "any", "exact_only": True, "allow_related": True},
            ]}}}
            path.write_text(json.dumps(v1), encoding="utf-8")
            lookup("word", config_path=path, client=client)
        self.assertEqual(
            client.candidate_policies,
            [("Exact", "variants"), ("Fuzzy", "all"), ("Exact", "fallback")],
        )

    def test_v1_config_allow_related_keeps_related_row(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "config.json"
            sources = [
                {"name": "Gcide", "when": "any", "exact_only": True, "allow_related": True},
                {"name": "Wikt", "when": "any", "exact_only": True, "allow_related": False},
                {"name": "Moe", "when": "cjk", "exact_only": False, "allow_related": False},
            ]
            for version in (1, 2):
                path.write_text(json.dumps({"version": version, "modes": {"en-en": {"sources": sources}}}),
                                encoding="utf-8")
                config, metadata = load_config(path)
                policy = [source_exact_only(source) for source in config["modes"]["en-en"]["sources"]]
                if version == 1:
                    self.assertEqual(policy, [False, True, True])
                    self.assertEqual(metadata["legacy_related_policy"], "v1_allow_related")
                else:
                    self.assertEqual(policy, [True, True, False])
                    self.assertNotIn("legacy_related_policy", metadata)

    def test_exact_homograph_prompt_is_aborted_and_every_entry_fetched(self):
        # words.hk lists 為 (wai4, wai6) behind a choice prompt even for an
        # exact search; an unanswered prompt used to block until timeout.
        calls = []
        options = b"[0)FixtureDict-->\xe7\x82\xba\n1)FixtureDict-->\xe7\x82\xba\n"
        head = "Enter word or phrase: 為\n".encode("utf-8")

        def protocol_runner(**kwargs):
            query, choice = kwargs["query"], kwargs["choice"]
            calls.append((query, choice))
            if query.startswith("/"):
                stdout = "Enter word or phrase: /為\n[]\nEnter word or phrase: \n".encode("utf-8")
                return {"returncode": 0, "termination_reason": None, "stdout_bytes": len(stdout),
                        "choice_prompt_count": 0, "process_group_reaped": True}, stdout
            if choice is None:
                stdout = head + options + b"Your choice[-1 to abort]: -1\n]\nEnter word or phrase: \n"
                return {"returncode": 0, "termination_reason": None, "stdout_bytes": len(stdout),
                        "choice_prompt_count": 1, "choice_aborted": True,
                        "process_group_reaped": True}, stdout
            entry = json.dumps({"dict": "FixtureDict", "word": "為", "definition": f"wai{choice}"},
                               ensure_ascii=False).encode("utf-8")
            stdout = (head + options + f"Your choice[-1 to abort]: {choice}\n".encode("ascii")
                      + entry + b"]\nEnter word or phrase: \n")
            return {"returncode": 0, "termination_reason": None, "stdout_bytes": len(stdout),
                    "choice_prompt_count": 1, "process_group_reaped": True}, stdout

        client = SdcvClient(executable="/bin/true", protocol_runner=protocol_runner)
        entries, warning = client.query({"name": "FixtureDict", "allow_related": False}, "為")
        self.assertIsNone(warning)
        self.assertEqual([entry["definition"] for entry in entries], ["wai0", "wai1"])
        self.assertEqual([choice for query, choice in calls if not query.startswith("/")], [None, 0, 1])

    def test_unanswered_choice_prompt_is_aborted_by_protocol_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = write_shell(
                pathlib.Path(directory) / "homograph-sdcv",
                """
                printf 'Enter word or phrase: '
                read -r word
                printf '%s\\n[0)FixtureDict-->%s\\n1)FixtureDict-->%s\\n' "$word" "$word" "$word"
                printf 'Your choice[-1 to abort]: '
                read -r choice
                printf '%s\\n]\\n' "$choice"
                printf 'Enter word or phrase: '
                read -r rest || true
                printf '\\n'
                """,
            )
            started = time.monotonic()
            run, stdout = run_bounded_protocol(
                name="sdcv", argv=[str(fake)], environment=dict(os.environ),
                query="為", choice=None, timeout_seconds=2.0,
            )
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertIsNone(run["termination_reason"])
            self.assertTrue(run["choice_aborted"])
            parsed = parse_protocol_stdout(stdout, query="為", choice=-1)
            self.assertEqual(parsed["entries"], [])
            self.assertEqual(len(parsed["options"]), 2)

    def test_external_tokenizer_receives_query_only_on_stdin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            argv_log = root / "argv"
            stdin_log = root / "stdin"
            tokenizer = write_shell(
                root / "tokenizer",
                """
                printf '%s\n' "$*" > "$TOKENIZER_ARGV_LOG"
                IFS= read -r query
                printf '%s' "$query" > "$TOKENIZER_STDIN_LOG"
                printf '{"tokens":["%s"]}\n' "$query"
                """,
            )
            query = "私密查詢"
            environment = {
                "DANK_LOOKUP_TOKENIZER": str(tokenizer),
                "TOKENIZER_ARGV_LOG": str(argv_log),
                "TOKENIZER_STDIN_LOG": str(stdin_log),
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                tokens, engine, warning = _segment(
                    query,
                    detect_features(query),
                    {},
                )
            self.assertEqual(tokens, [query])
            self.assertEqual(engine, "external-tokenizer")
            self.assertIsNone(warning)
            self.assertEqual(stdin_log.read_text(encoding="utf-8"), query)
            argv = argv_log.read_text(encoding="utf-8")
            self.assertIn("--stdin", argv)
            self.assertNotIn(query, argv)

    def test_external_tokenizer_streams_stdout_and_stderr_caps_then_reaps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            tokenizer = root / "tokenizer"
            tokenizer.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/python3
                    import os
                    import pathlib
                    import sys

                    sys.stdin.readline()
                    pid = os.getpid()
                    pathlib.Path(os.environ["TOKENIZER_PID_FILE"]).write_text(str(pid), encoding="ascii")
                    progress = pathlib.Path(os.environ["TOKENIZER_PROGRESS_FILE"])
                    progress_fd = os.open(progress, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                    output = 2 if os.environ["TOKENIZER_STREAM"] == "stderr" else 1
                    chunk = b"x" * (64 * 1024)
                    written = 0
                    while True:
                        os.write(output, chunk)
                        written += len(chunk)
                        os.write(progress_fd, f"{written}\\n".encode("ascii"))
                    """
                ),
                encoding="utf-8",
            )
            tokenizer.chmod(0o755)
            for stream in ("stdout", "stderr"):
                with self.subTest(stream=stream):
                    pid_file = root / f"{stream}.pid"
                    progress_file = root / f"{stream}.progress"
                    environment = {
                        "DANK_LOOKUP_TOKENIZER": str(tokenizer),
                        "TOKENIZER_PID_FILE": str(pid_file),
                        "TOKENIZER_PROGRESS_FILE": str(progress_file),
                        "TOKENIZER_STREAM": stream,
                    }
                    started = time.monotonic()
                    with mock.patch.dict(os.environ, environment, clear=False):
                        tokens, engine, warning = _segment(
                            "私密查詢",
                            detect_features("私密查詢"),
                            {},
                            timeout_seconds=2.0,
                        )
                    duration = time.monotonic() - started
                    self.assertLess(
                        duration,
                        1.5,
                        f"{stream} reached timeout instead of its byte cap",
                    )
                    self.assertNotEqual(engine, "external-tokenizer")
                    self.assertIsNotNone(warning)
                    self.assertTrue(tokens)
                    pid = int(pid_file.read_text(encoding="ascii"))
                    self.assertFalse(pathlib.Path(f"/proc/{pid}").exists())
                    observed = max(
                        int(line)
                        for line in progress_file.read_text(encoding="ascii").splitlines()
                        if line.isdigit()
                    )
                    self.assertGreaterEqual(observed, 896 * 1024)
                    self.assertLessEqual(observed, 2 * 1024 * 1024)


class SdcvStdinProbeParserTests(unittest.TestCase):
    def test_exact_json_frame_has_unique_terminal_state(self):
        stdout = (
            b"Enter word or phrase: word\n"
            b'[{"dict":"Fixture","word":"word","definition":"exact"}]\n'
            b"Enter word or phrase: \n"
        )
        parsed = parse_protocol_stdout(stdout, query="word")
        self.assertEqual(parsed["entries"][0]["definition"], "exact")
        self.assertEqual(
            parsed["decisions"],
            [
                "main_prompt",
                "query_echo",
                "json_result",
                "terminal_main_prompt",
                "stdin_eof",
            ],
        )

    def test_fuzzy_option_frame_requires_explicit_choice_echo(self):
        stdout = (
            b"Enter word or phrase: /wrod\n"
            b"[0)Fixture-->word\n"
            b"1)Fixture-->world\n"
            b"Your choice[-1 to abort]: 0\n"
            b'{"dict":"Fixture","word":"word","definition":"exact"}]\n'
            b"Enter word or phrase: \n"
        )
        parsed = parse_protocol_stdout(stdout, query="/wrod", choice=0)
        self.assertEqual([item["word"] for item in parsed["options"]], ["word", "world"])
        self.assertEqual(parsed["entries"][0]["word"], "word")

    def test_incomplete_or_ambiguous_frames_are_rejected(self):
        with self.assertRaises(SdcvProtocolError):
            parse_protocol_stdout(
                b"Enter word or phrase: /wrod\n[0)Fixture-->word\n",
                query="/wrod",
                choice=0,
            )
        with self.assertRaises(SdcvProtocolError):
            parse_empty_eof(b"Enter word or phrase: ")

    def test_protocol_argv_contains_only_metadata(self):
        argv = protocol_argv(
            "sdcv",
            data_dir="/private/data",
            dictionaries=["Fixture"],
            exact=True,
        )
        self.assertNotIn("private-query", argv)
        self.assertEqual(argv[-2:], ["--use-dict", "Fixture"])


class RenderingTests(unittest.TestCase):
    def test_inline_full_limit_always_fits_open_text_utf8_budget(self):
        _entry, _body, inline = _preview_limits(
            {"preview": {"max_inline_full_chars": 99 * 1024 * 1024}}
        )
        self.assertEqual(inline, MAX_INLINE_FULL_CHARS)
        self.assertLessEqual(inline * 4, 8 * 1024 * 1024)

    def test_conservative_markup_cleanup(self):
        value = "<b>head</b><br>line &amp; sense\x00\n\n\nexample"
        cleaned = clean_definition("Example", value, definition_format="html")
        self.assertIn("head\nline & sense", cleaned)
        self.assertIn("\n\nexample", cleaned)
        self.assertNotIn("<b>", cleaned)
        self.assertNotIn("\x00", cleaned)

    def test_plain_cleanup_preserves_angle_bracketed_prose(self):
        value = "Use vector<T> & keep <not-markup> literally.\x00"
        cleaned = clean_definition("Example", value, definition_format="plain")
        self.assertEqual(cleaned, "Use vector<T> & keep <not-markup> literally.")

    def test_long_entry_preview_is_explicit_and_full_text_is_single_copy(self):
        tail = "UNIQUE-COMPLETE-TAIL"
        definition = "sense " * 20_000 + tail
        rendered, vault = render_entries(
            [{"dict": "LongDict", "word": "long", "definition": definition}],
            max_entry_chars=200,
            max_body_chars=400,
            max_inline_full_chars=len(definition) + 100,
            retain_entry_vault=True,
        )
        self.assertTrue(rendered["truncated"])
        self.assertIn(TRUNCATION_MARKER, rendered["body"])
        self.assertTrue(rendered["full_body"].endswith(tail))
        self.assertNotIn("full_body", rendered["entries"][0])
        encoded = json.dumps(rendered, ensure_ascii=False)
        self.assertEqual(encoded.count(tail), 1)
        self.assertLess(len(encoded), len(definition) + 10_000)
        identifier = rendered["entries"][0]["raw_ref"]
        self.assertTrue(vault["entries"][identifier]["full_body"].endswith(tail))
        self.assertNotIn("raw_definition", vault["entries"][identifier])

    def test_nine_mib_entry_uses_bounded_json_and_complete_full_reference(self):
        tail = "NINE-MIB-COMPLETE-TAIL"
        definition = "x" * (9 * 1024 * 1024) + tail
        rendered, vault = render_entries(
            [{"dict": "GiantDict", "word": "giant", "definition": definition}],
            max_entry_chars=200,
            max_body_chars=400,
            max_inline_full_chars=256 * 1024,
            retain_full_blobs=True,
        )
        self.assertIsNone(rendered["full_body"])
        self.assertIsNotNone(rendered["full_ref"])
        self.assertEqual(rendered["full_chars"], len(rendered["full_body"] or vault["full_blobs"][rendered["full_ref"]]))
        self.assertTrue(vault["full_blobs"][rendered["full_ref"]].endswith(tail))
        self.assertLess(len(json.dumps(rendered)), 100_000)

    def test_normal_render_does_not_build_private_retention(self):
        rendered, vault = render_entries(
            [{"dict": "Plain", "word": "word", "definition": "definition"}]
        )
        self.assertEqual(rendered["full_body"], "[Plain]\nword\ndefinition")
        self.assertEqual(vault, {"entries": {}, "full_blobs": {}})


class DefinitionFormatTests(unittest.TestCase):
    @staticmethod
    def _ifo(
        directory: pathlib.Path,
        filename: str,
        bookname: str,
        sequence: str | None,
    ) -> None:
        lines = ["StarDict's dict ifo file", "version=2.4.2", f"bookname={bookname}"]
        if sequence is not None:
            lines.append(f"sametypesequence={sequence}")
        (directory / filename).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_explicit_override_has_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self._ifo(root, "fixture.ifo", "Fixture", "m")
            config = {"stardict_data_dir": [str(root)]}
            self.assertEqual(
                resolve_definition_format(
                    {"name": "Fixture", "definition_format": "html"}, config
                ),
                "html",
            )
            self.assertEqual(
                resolve_definition_format(
                    {"name": "Fixture", "definition_format": "plain"}, config
                ),
                "plain",
            )
            self.assertEqual(
                resolve_definition_format(
                    {"name": "Fixture", "definition_format": "invalid"}, config
                ),
                "plain",
            )

    def test_ifo_exact_bookname_and_sametype_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self._ifo(root, "html.ifo", "HTML Fixture", "h")
            self._ifo(root, "plain.ifo", "Plain Fixture", "m")
            self._ifo(root, "other.ifo", "Other Fixture", "h")
            config = {"stardict_data_dir": [str(root)]}
            self.assertEqual(
                resolve_definition_format({"name": "HTML Fixture"}, config), "html"
            )
            self.assertEqual(
                resolve_definition_format({"name": "Plain Fixture"}, config), "plain"
            )
            self.assertEqual(
                resolve_definition_format({"name": "html fixture"}, config), "plain"
            )

    def test_conflict_mixed_missing_and_no_sametype_fall_back_plain(self):
        cases = {
            "conflict": [("a.ifo", "h"), ("b.ifo", "m")],
            "mixed": [("a.ifo", "mh")],
            "missing_sequence": [("a.ifo", None)],
            "unknown": [("a.ifo", "x")],
            "missing_ifo": [],
        }
        for label, fixtures in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                for filename, sequence in fixtures:
                    self._ifo(root, filename, "Fixture", sequence)
                self.assertEqual(
                    resolve_definition_format(
                        {"name": "Fixture"}, {"stardict_data_dir": [str(root)]}
                    ),
                    "plain",
                )

    def test_lookup_applies_resolved_source_format(self):
        cfg = config_for_tests()
        query = "word"
        client = FakeClient(
            installed={"EnDict"},
            responses={
                ("EnDict", query): [
                    {
                        "dict": "EnDict",
                        "word": query,
                        "definition": "<b>head</b><br>vector<T>",
                    }
                ]
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self._ifo(root, "en.ifo", "EnDict", "h")
            cfg["stardict_data_dir"] = [str(root)]
            config_path = root / "config.json"
            config_path.write_text(json.dumps(cfg), encoding="utf-8")
            result, _vault = lookup(query, config_path=config_path, client=client)
        body = result["results_by_mode"]["en"]["body"]
        self.assertIn("head\nvector", body)
        self.assertNotIn("<b>", body)

    def test_timing_and_request_identity_are_monotonic_contract_fields(self):
        cfg = config_for_tests()
        client = FakeClient(
            installed={"EnDict"},
            responses={("EnDict", "word"): [{"dict": "EnDict", "word": "word", "definition": "definition"}]},
        )
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "config.json"
            path.write_text(json.dumps(cfg), encoding="utf-8")
            result, _ = lookup("word", config_path=path, request_id="req-1", sequence=42, client=client)
        self.assertEqual(result["request_id"], "req-1")
        self.assertEqual(result["sequence"], 42)
        self.assertGreater(result["timing"]["started_monotonic_ns"], 0)
        self.assertGreaterEqual(result["timing"]["total_ms"], 0)
        self.assertGreaterEqual(result["timing"]["backend_ms"], 0)


if __name__ == "__main__":
    unittest.main()
