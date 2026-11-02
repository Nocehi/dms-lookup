#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import uuid


ROOT = pathlib.Path(__file__).resolve().parent.parent
CLI = ROOT / "bin/dank-lookup"
BACKEND = ROOT / "lib/backends/sdcv.py"
SELECTION = ROOT / "bin/dank-lookup-selection"
TEST_PROCESS_DEADLINE_SECONDS = 10.0


def write_executable(path: pathlib.Path, body: str) -> pathlib.Path:
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    path.chmod(0o755)
    return path


def wait_for_path(path: pathlib.Path, timeout: float = 5.0) -> pathlib.Path:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return path
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path}")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.marker = self.root / "injected"
        self.fake = write_executable(
            self.root / "fake-sdcv",
            """\
            #!/bin/sh
            set -eu
            for argument do
                if [ "$argument" = "--list-dicts" ]; then
                    printf "%s\\n" "Dictionary's name   Word count" "FixtureDict    2"
                    exit 0
                fi
            done
            # Like sdcv 0.5.5's interactive loop: answer queries until stdin
            # EOF (one process serves every frame of a lookup session).
            while :; do
                printf '%s' 'Enter word or phrase: '
                IFS= read -r query || { printf '\\n'; exit 0; }
                printf '%s\\n' "$query"
                if [ -n "${FAKE_INVALID:-}" ]; then
                    printf '%s\\n' 'not json'
                elif [ -n "${FAKE_NO_HIT:-}" ]; then
                    printf '%s\\n' '[]'
                else
                    printf '[{"dict":"FixtureDict","word":"%s","definition":"safe definition"}]\\n' "$query"
                fi
            done
            """,
        )
        self.giant_fake = write_executable(
            self.root / "giant-sdcv",
            """\
            #!/usr/bin/env python3
            import json
            import os
            import sys

            if "--list-dicts" in sys.argv:
                print("Dictionary's name   Word count")
                print("FixtureDict    2")
                raise SystemExit(0)
            # Answer queries until stdin EOF, like sdcv 0.5.5.
            size = int(os.environ["FAKE_DEFINITION_SIZE"])
            while True:
                sys.stdout.write("Enter word or phrase: ")
                sys.stdout.flush()
                line = sys.stdin.readline()
                if not line:
                    print()
                    break
                query = line.rstrip("\\n")
                sys.stdout.write(query + "\\n")
                definition = "x" * size + "GIANT-COMPLETE-TAIL"
                print(json.dumps([{"dict": "FixtureDict", "word": query, "definition": definition}]))
                sys.stdout.flush()
            """,
        )
        self.config = self.root / "config.json"
        self.config.write_text(
            json.dumps(
                {
                    "version": 2,
                    "source_timeout_ms": 5000,
                    "lookup_timeout_ms": 10000,
                    "modes": {
                        "en": {
                            "label": "English",
                            "default_for": ["latin", "unknown"],
                            "sources": [{"name": "FixtureDict", "when": "any", "allow_related": False}],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if not list(self.root.rglob("backend.sock")):
                break
            time.sleep(0.01)
        self.temporary.cleanup()

    def env(self):
        value = os.environ.copy()
        value.update(
            {
                "DANK_LOOKUP_SDCV": str(self.fake),
                "DANK_LOOKUP_CONFIG": str(self.config),
                "DANK_LOOKUP_LIBDIR": str(ROOT / "lib"),
                "XDG_RUNTIME_DIR": str(self.runtime),
                "DANK_LOOKUP_BACKEND_IDLE_TIMEOUT": "0.1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        return value

    def run_cli(self, *arguments, env=None, input_bytes=None):
        return subprocess.run(
            [sys.executable, str(CLI), *arguments],
            input=input_bytes,
            capture_output=True,
            env=env or self.env(),
            timeout=TEST_PROCESS_DEADLINE_SECONDS,
        )

    def run_backend(self, *arguments, env=None, input_bytes=None):
        return subprocess.run(
            [sys.executable, str(BACKEND), *arguments],
            input=input_bytes,
            capture_output=True,
            env=env or self.env(),
            timeout=TEST_PROCESS_DEADLINE_SECONDS,
        )

    def test_legacy_backend_adapter_accepts_bounded_stdin(self):
        query = b"adapter private query"
        result = self.run_backend("--stdout", "--stdin", input_bytes=query)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        data = json.loads(result.stdout)
        self.assertEqual(data["query"], query.decode())
        self.assertEqual(data["title"], query.decode())
        self.assertIn("safe definition", data["results_by_mode"]["en"]["body"])

    def test_legacy_backend_adapter_rejects_mixed_stdin_and_argv_query(self):
        result = self.run_backend("--stdout", "--stdin", "argv-query", input_bytes=b"stdin-query")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(b"stdin-query", result.stderr)

    def test_lookup_stdout_and_no_default_history(self):
        query = f"$(touch {self.marker})'; echo unsafe"
        request_id = str(uuid.uuid4())
        result = self.run_cli(
            "lookup",
            "--request-id",
            request_id,
            "--sequence",
            "1",
            input_bytes=query.encode("utf-8"),
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        data = json.loads(result.stdout)
        self.assertEqual(data["query"], query)
        self.assertFalse(data["runtime"]["retained"])
        self.assertFalse(self.marker.exists())
        runtime_root = self.runtime / "dank-lookup"
        self.assertFalse(any(path.name.startswith("request-") for path in runtime_root.iterdir()))

    def test_lookup_rejects_positional_query(self):
        result = self.run_cli("lookup", "argv-query", input_bytes=b"stdin-query")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(b"stdin-query", result.stderr)

    def test_missing_sdcv_and_invalid_json_are_non_crashing_structured_results(self):
        missing_env = self.env()
        missing_runtime = self.root / "runtime-missing"
        missing_runtime.mkdir(mode=0o700)
        missing_env["XDG_RUNTIME_DIR"] = str(missing_runtime)
        missing_env["DANK_LOOKUP_SDCV"] = str(self.root / "missing-sdcv")
        missing = self.run_cli("lookup", env=missing_env, input_bytes=b"word")
        self.assertEqual(json.loads(missing.stdout)["error"]["code"], "sdcv_missing")

        invalid_env = self.env()
        invalid_runtime = self.root / "runtime-invalid"
        invalid_runtime.mkdir(mode=0o700)
        invalid_env["XDG_RUNTIME_DIR"] = str(invalid_runtime)
        invalid_env["FAKE_INVALID"] = "1"
        invalid = self.run_cli("lookup", env=invalid_env, input_bytes=b"word")
        data = json.loads(invalid.stdout)
        self.assertIn("sdcv_invalid_json", data["warnings"])
        self.assertEqual(data["available_modes"], [])
        self.assertEqual(data["error"]["code"], "backend_error")

        no_hit_env = self.env()
        no_hit_runtime = self.root / "runtime-no-hit"
        no_hit_runtime.mkdir(mode=0o700)
        no_hit_env["XDG_RUNTIME_DIR"] = str(no_hit_runtime)
        no_hit_env["FAKE_NO_HIT"] = "1"
        no_hit = self.run_cli("lookup", env=no_hit_env, input_bytes=b"clean-no-hit")
        data = json.loads(no_hit.stdout)
        self.assertEqual(data["available_modes"], [])
        self.assertEqual(data["warnings"], [])
        self.assertIsNone(data["error"])

    def test_cli_discovers_config_under_custom_xdg_config_home(self):
        xdg_config = self.root / "xdg-config"
        installed_config = xdg_config / "dank-lookup/config.json"
        installed_config.parent.mkdir(parents=True)
        installed_config.write_text(self.config.read_text(encoding="utf-8"), encoding="utf-8")
        env = self.env()
        env.pop("DANK_LOOKUP_CONFIG", None)
        env["XDG_CONFIG_HOME"] = str(xdg_config)
        result = self.run_cli("lookup", env=env, input_bytes=b"word")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        data = json.loads(result.stdout)
        self.assertTrue(data["config"]["loaded"])
        self.assertEqual(pathlib.Path(data["config"]["path"]), installed_config)

    def test_open_text_uses_argv_and_private_file(self):
        opener = self.root / "fake-opener"
        opened = self.root / "opened-path"
        write_executable(
            opener,
            """\
            #!/bin/sh
            set -eu
            printf '%s' "$1" > "$OPENED_PATH"
            """,
        )
        env = self.env()
        env["DANK_LOOKUP_OPENER"] = str(opener)
        env["DANK_LOOKUP_FULL_TTL_SECONDS"] = "2.0"
        env["OPENED_PATH"] = str(opened)
        result = self.run_cli("open-text", "--request-id", "open-1", env=env, input_bytes=b"complete body")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        destination = pathlib.Path(wait_for_path(opened).read_text(encoding="utf-8"))
        self.assertEqual(destination.read_bytes(), b"complete body")
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(destination.parent.stat().st_mode), 0o700)

    def test_nine_mib_lookup_json_is_bounded_and_open_ref_is_complete(self):
        opener = self.root / "giant-opener"
        opened = self.root / "giant-opened-path"
        write_executable(
            opener,
            """\
            #!/bin/sh
            set -eu
            printf '%s' "$1" > "$OPENED_PATH"
            """,
        )
        env = self.env()
        env.update(
            {
                "DANK_LOOKUP_SDCV": str(self.giant_fake),
                "FAKE_DEFINITION_SIZE": str(9 * 1024 * 1024),
                "DANK_LOOKUP_OPENER": str(opener),
                "DANK_LOOKUP_FULL_TTL_SECONDS": "10.0",
                "OPENED_PATH": str(opened),
            }
        )
        giant_request_id = str(uuid.uuid4())
        lookup_result = self.run_cli(
            "lookup",
            "--request-id",
            giant_request_id,
            "--sequence",
            "9",
            env=env,
            input_bytes=b"giant",
        )
        self.assertEqual(lookup_result.returncode, 0, lookup_result.stderr.decode())
        self.assertLess(len(lookup_result.stdout), 100_000)
        data = json.loads(lookup_result.stdout)
        mode = data["results_by_mode"]["en"]
        self.assertIsNone(mode["full_body"])
        self.assertTrue(mode["full_ref"])
        self.assertFalse(data["runtime"]["retained"])
        self.assertFalse(
            any(
                path.name.startswith("request-")
                for path in (self.runtime / "dank-lookup").iterdir()
            )
        )
        open_result = self.run_cli(
            "open-ref",
            "--request-id",
            giant_request_id,
            "--ref",
            mode["full_ref"],
            env=env,
            input_bytes=b"giant",
        )
        self.assertEqual(open_result.returncode, 0, open_result.stderr.decode())
        destination = pathlib.Path(wait_for_path(opened).read_text(encoding="utf-8"))
        self.assertTrue(destination.read_text(encoding="utf-8").endswith("GIANT-COMPLETE-TAIL"))
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)

    def test_selection_launcher_uses_metadata_ipc_and_capture_keeps_query_off_argv(self):
        command_dir = self.root / "commands"
        command_dir.mkdir()
        log = self.root / "ipc-log.jsonl"
        quickshell = command_dir / "quickshell"
        write_executable(
            quickshell,
            """\
            #!/bin/sh
            set -eu
            for argument do
                printf '%s\\000' "$argument" >> "$IPC_LOG"
            done
            printf '\\036' >> "$IPC_LOG"
            for argument do
                if [ "$argument" = "prepare" ]; then
                    printf '%s\\n' 'Ret: "selection-request"'
                    exit 0
                fi
            done
            """,
        )
        wl_paste = command_dir / "wl-paste"
        write_executable(
            wl_paste,
            """\
            #!/bin/sh
            set -eu
            printf '%s' "$FAKE_SELECTION"
            """,
        )
        query = f"$(touch {self.marker}) ' quoted"
        env = self.env()
        env.update(
            {
                "PATH": str(command_dir) + os.pathsep + env["PATH"],
                "IPC_LOG": str(log),
                "FAKE_SELECTION": query,
            }
        )
        result = subprocess.run(
            ["bash", str(SELECTION), "selection"],
            capture_output=True,
            env=env,
            timeout=TEST_PROCESS_DEADLINE_SECONDS,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        calls = [
            [part.decode("utf-8") for part in record.rstrip(b"\0").split(b"\0")]
            for record in log.read_bytes().split(b"\x1e")
            if record
        ]
        self.assertIn("prepare", calls[0])
        self.assertIn("capturePrepared", calls[1])
        self.assertNotIn(query, [argument for call in calls for argument in call])

        capture = subprocess.run(
            ["bash", str(SELECTION), "--capture"],
            capture_output=True,
            env=env,
            timeout=TEST_PROCESS_DEADLINE_SECONDS,
        )
        self.assertEqual(capture.returncode, 0, capture.stderr.decode())
        version, status, kind, primary, clipboard, captured_query = (
            capture.stdout.decode().split("\n", 5)
        )
        self.assertEqual(version, "capture-v1")
        self.assertEqual(status, "success_nonempty")
        self.assertEqual(kind, "primary")
        self.assertEqual(primary, "success_nonempty")
        self.assertEqual(clipboard, "not_attempted")
        self.assertEqual(captured_query, query)
        self.assertFalse(self.marker.exists())

    def test_selection_capture_classifies_outcomes_and_validates_timeouts(self):
        command_dir = self.root / "selection-commands"
        command_dir.mkdir()
        call_log = self.root / "selection-calls"
        write_executable(
            command_dir / "wl-paste",
            """\
            #!/bin/sh
            set -eu
            kind=clipboard
            for argument do
                if [ "$argument" = "--primary" ]; then
                    kind=primary
                fi
            done
            printf '%s\\n' "$kind" >> "$CAPTURE_CALL_LOG"
            if [ "$kind" = primary ]; then
                mode=$FAKE_PRIMARY_MODE
                value=${FAKE_PRIMARY_VALUE:-}
            else
                mode=$FAKE_CLIPBOARD_MODE
                value=${FAKE_CLIPBOARD_VALUE:-}
            fi
            case "$mode" in
                nonempty) printf '%s' "$value" ;;
                empty) : ;;
                error) exit 7 ;;
                timeout) sleep 0.2 ;;
                *) exit 9 ;;
            esac
            """,
        )

        def capture(primary, clipboard, primary_value="", clipboard_value=""):
            call_log.unlink(missing_ok=True)
            environment = self.env()
            environment.update(
                {
                    "PATH": str(command_dir) + os.pathsep + environment["PATH"],
                    "CAPTURE_CALL_LOG": str(call_log),
                    "FAKE_PRIMARY_MODE": primary,
                    "FAKE_CLIPBOARD_MODE": clipboard,
                    "FAKE_PRIMARY_VALUE": primary_value,
                    "FAKE_CLIPBOARD_VALUE": clipboard_value,
                    "DANK_LOOKUP_PRIMARY_TIMEOUT_MS": "20",
                    "DANK_LOOKUP_CLIPBOARD_TIMEOUT_MS": "20",
                }
            )
            completed = subprocess.run(
                ["bash", str(SELECTION), "--capture"],
                capture_output=True,
                env=environment,
                timeout=TEST_PROCESS_DEADLINE_SECONDS,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            self.assertEqual(completed.stderr, b"")
            return completed.stdout.decode().split("\n", 5)

        primary = capture("nonempty", "error", primary_value="primary text")
        self.assertEqual(primary[1:], [
            "success_nonempty", "primary", "success_nonempty", "not_attempted", "primary text"
        ])
        self.assertEqual(call_log.read_text(encoding="utf-8").splitlines(), ["primary"])

        fallback = capture("empty", "nonempty", clipboard_value="clipboard text")
        self.assertEqual(fallback[1:], [
            "success_nonempty", "clipboard", "success_empty", "success_nonempty", "clipboard text"
        ])

        whitespace = capture("nonempty", "empty", primary_value=" \t ")
        self.assertEqual(whitespace[1:5], [
            "success_empty", "none", "success_whitespace", "success_empty"
        ])

        timed_out = capture("timeout", "empty")
        self.assertEqual(timed_out[1:5], [
            "success_empty", "none", "timeout", "success_empty"
        ])

        failed = capture("error", "error")
        self.assertEqual(failed[1:5], [
            "command_error", "none", "command_error", "command_error"
        ])

        invalid_environment = self.env()
        invalid_environment.update(
            {
                "PATH": str(command_dir) + os.pathsep + invalid_environment["PATH"],
                "DANK_LOOKUP_PRIMARY_TIMEOUT_MS": "5001",
            }
        )
        invalid = subprocess.run(
            ["bash", str(SELECTION), "--capture"],
            capture_output=True,
            env=invalid_environment,
            timeout=TEST_PROCESS_DEADLINE_SECONDS,
        )
        self.assertEqual(invalid.returncode, 2)
        self.assertIn(b"1 to 5000", invalid.stderr)

        utilities = self.root / "capture-utilities"
        utilities.mkdir()
        for name in ("timeout", "sed", "tr"):
            (utilities / name).symlink_to(pathlib.Path("/usr/bin") / name)
        unavailable_environment = self.env()
        unavailable_environment["PATH"] = str(utilities)
        unavailable = subprocess.run(
            ["/usr/bin/bash", str(SELECTION), "--capture"],
            capture_output=True,
            env=unavailable_environment,
            timeout=TEST_PROCESS_DEADLINE_SECONDS,
        )
        self.assertEqual(unavailable.returncode, 0, unavailable.stderr.decode())
        self.assertEqual(
            unavailable.stdout.decode().split("\n", 5)[1:5],
            ["unavailable", "none", "unavailable", "unavailable"],
        )

    def test_selection_capture_streams_text_with_a_hard_cap_and_reaps_producers(self):
        command_dir = self.root / "bounded-selection-commands"
        command_dir.mkdir()
        pid_log = self.root / "selection-pids"
        progress_dir = self.root / "selection-progress"
        progress_dir.mkdir()
        write_executable(
            command_dir / "wl-paste",
            """\
            #!/usr/bin/python3
            import os
            import pathlib
            import sys
            import time

            pid = os.getpid()
            with open(os.environ["FAKE_PID_LOG"], "a", encoding="ascii") as handle:
                handle.write(f"{pid} {' '.join(sys.argv[1:])}\\n")
            progress = pathlib.Path(os.environ["FAKE_PROGRESS_DIR"]) / str(pid)
            progress_fd = os.open(progress, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            written = 0
            chunk = b"x" * 4096
            while True:
                os.write(1, chunk)
                written += len(chunk)
                os.write(progress_fd, f"{written}\\n".encode("ascii"))
                time.sleep(0.005)
            """,
        )
        environment = self.env()
        environment.update(
            {
                "PATH": str(command_dir) + os.pathsep + environment["PATH"],
                "FAKE_PID_LOG": str(pid_log),
                "FAKE_PROGRESS_DIR": str(progress_dir),
                "DANK_LOOKUP_PRIMARY_TIMEOUT_MS": "1000",
                "DANK_LOOKUP_CLIPBOARD_TIMEOUT_MS": "1000",
            }
        )

        started = time.monotonic()
        completed = subprocess.run(
            ["bash", str(SELECTION), "--capture"],
            capture_output=True,
            env=environment,
            timeout=TEST_PROCESS_DEADLINE_SECONDS,
        )
        duration = time.monotonic() - started
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertLess(duration, 1.0, "capture reached timeout instead of the byte cap")
        self.assertEqual(
            completed.stdout.decode().split("\n", 5)[1:5],
            ["too_large", "none", "too_large", "too_large"],
        )
        records = [line.split() for line in pid_log.read_text(encoding="ascii").splitlines()]
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertIn("--type", record)
            self.assertIn("text", record)
            pid = int(record[0])
            self.assertFalse(pathlib.Path(f"/proc/{pid}").exists(), f"selection pid {pid} survived")
            observed = max(
                int(line)
                for line in (progress_dir / str(pid)).read_text(encoding="ascii").splitlines()
                if line.isdigit()
            )
            self.assertGreaterEqual(observed, 12 * 1024)
            self.assertLessEqual(observed, 128 * 1024)

    def test_backend_request_cold_starts_fixed_service_for_all_lookup_actions(self):
        environment = self.env()
        environment["DANK_LOOKUP_BACKEND_IDLE_TIMEOUT"] = "0.1"
        actions = ("initial_selection", "mode_change", "source_change", "relookup")
        for sequence, action in enumerate(actions, 90):
            envelope = {
                "action": action,
                "request_id": str(uuid.uuid4()),
                "sequence": sequence,
                "source": "primary",
                "mode": "en",
                "query": "word",
            }
            result = self.run_cli(
                "backend-request",
                "--timeout",
                "2.0",
                env=environment,
                input_bytes=json.dumps(envelope).encode("utf-8"),
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            response = json.loads(result.stdout)
            self.assertEqual(response["request_id"], envelope["request_id"])
            self.assertEqual(response["sequence"], sequence)
            self.assertEqual(response["transport"]["action"], action)

        socket_path = self.runtime / "dank-lookup/backend.sock"
        deadline = time.monotonic() + 2.0
        while socket_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(socket_path.exists())

    def test_backend_request_reads_envelope_from_stdin(self):
        environment = self.env()
        service = subprocess.Popen(
            [
                sys.executable,
                str(CLI),
                "backend-serve",
                "--idle-timeout",
                "0.3",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        query = "word"
        envelope = {
            "action": "initial_selection",
            "request_id": str(uuid.uuid4()),
            "sequence": 81,
            "source": "selection",
            "mode": "en",
            "query": query,
        }
        result = None
        try:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if service.poll() is not None:
                    break
                if (self.runtime / "dank-lookup/backend.sock").is_socket():
                    result = self.run_cli(
                        "backend-request",
                        "--timeout",
                        "1.0",
                        env=environment,
                        input_bytes=json.dumps(envelope).encode("utf-8"),
                    )
                    if result.returncode == 0:
                        break
                time.sleep(0.01)
            self.assertIsNotNone(result)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            response = json.loads(result.stdout)
            self.assertEqual(response["request_id"], envelope["request_id"])
            self.assertEqual(response["sequence"], 81)
            self.assertEqual(response["query"], query)
            self.assertEqual(response["transport"]["action"], "initial_selection")
            self.assertNotIn(query, service.args)
        finally:
            try:
                service.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                service.kill()
                service.wait(timeout=1.0)
            if service.stdout is not None:
                service.stdout.close()
            if service.stderr is not None:
                service.stderr.close()


if __name__ == "__main__":
    unittest.main()
