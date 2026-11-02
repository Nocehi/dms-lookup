#!/usr/bin/env python3
"""Run the standalone QML behavior contract in an isolated offscreen shell.

The harness copies the canonical standalone sources into a temporary config,
uses only explicit mock IPC, and directs Copy/Open processes to private fake
sinks.  It never reads a clipboard, selection, live config, or live shell.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable

from quickshell_offscreen import launch_arguments, offscreen_environment


ROOT = pathlib.Path(__file__).resolve().parent.parent
QML_ROOT = ROOT / "qml"
SHELL = ROOT / "standalone/shell.qml"
TARGET = "dank-lookup"
DEFAULT_TEST_DEADLINE_SECONDS = 30.0


class SmokeFailure(RuntimeError):
    """An assertion or isolated frontend operation failed."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def decode_return(stdout: str) -> str:
    value = stdout.strip()
    if value.startswith("Ret:"):
        value = value[4:].strip()
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return value
    return decoded if isinstance(decoded, str) else json.dumps(decoded, ensure_ascii=False)


def parse_object(value: str, label: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SmokeFailure(f"{label} returned invalid JSON: {value[:240]}") from exc
    require(isinstance(decoded, dict), f"{label} did not return an object")
    return decoded


def assert_no_standalone_qs_imports() -> None:
    forbidden = re.compile(r"^\s*import\s+qs(?:\.|\s|$)", re.MULTILINE)
    paths = [SHELL, *sorted(QML_ROOT.rglob("*.qml"))]
    paths.extend(sorted((ROOT / "standalone/qml").rglob("*.qml")))
    offenders = [str(path.relative_to(ROOT)) for path in paths
                 if forbidden.search(path.read_text(encoding="utf-8"))]
    require(not offenders, "standalone imports DMS qs modules: " + ", ".join(offenders))


def write_fake_processes(
    directory: pathlib.Path,
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    backend = directory / "fake-dank-lookup"
    clipboard = directory / "fake-wl-copy"
    selection = directory / "fake-dank-lookup-selection"
    backend.write_text(
        """#!/bin/sh
set -eu
umask 077
if [ "${1-}" = "backend-request" ]; then
    capture=$DANK_LOOKUP_SMOKE_TRANSPORT_CAPTURE
    argv_tmp=${capture}.argv.$$
    stdin_tmp=${capture}.stdin.$$
    : > "$argv_tmp"
    for argument do
        printf '%s\\000' "$argument" >> "$argv_tmp"
    done
    cat > "$stdin_tmp"
    mv "$argv_tmp" "${capture}.argv"
    mv "$stdin_tmp" "${capture}.stdin"
    : > "$capture"
    if [ -n "${DANK_LOOKUP_SMOKE_BACKEND_RESPONSE-}" ] \\
            && [ -f "$DANK_LOOKUP_SMOKE_BACKEND_RESPONSE" ]; then
        cat "$DANK_LOOKUP_SMOKE_BACKEND_RESPONSE"
    else
        printf '%s\\n' '{}'
    fi
    exit 0
fi
capture=$DANK_LOOKUP_SMOKE_OPEN_CAPTURE
argv_tmp=${capture}.argv.$$
stdin_tmp=${capture}.stdin.$$
: > "$argv_tmp"
for argument do
    printf '%s\\000' "$argument" >> "$argv_tmp"
done
cat > "$stdin_tmp"
mv "$argv_tmp" "${capture}.argv"
mv "$stdin_tmp" "${capture}.stdin"
: > "$capture"
""",
        encoding="utf-8",
    )
    clipboard.write_text(
        """#!/bin/sh
set -eu
umask 077
capture=$DANK_LOOKUP_SMOKE_COPY_CAPTURE
temporary=${capture}.$$
cat > "$temporary"
mv "$temporary" "$capture"
""",
        encoding="utf-8",
    )
    selection.write_text(
        """#!/bin/sh
set -eu
test "${1-}" = "--capture"
if [ -f "$DANK_LOOKUP_SMOKE_EMPTY_SELECTION_FLAG" ]; then
    printf '%s\\n%s\\n%s\\n%s\\n%s\\n' \
        'capture-v1' 'success_empty' 'none' 'success_empty' 'success_empty'
else
    printf '%s\\n%s\\n%s\\n%s\\n%s\\n%s' \
        'capture-v1' 'success_nonempty' 'primary' 'success_nonempty' \
        'not_attempted' "$DANK_LOOKUP_SMOKE_SELECTION"
fi
""",
        encoding="utf-8",
    )
    backend.chmod(0o700)
    clipboard.chmod(0o700)
    selection.chmod(0o700)
    return backend, clipboard, selection


def wait_for(
    description: str,
    timeout: float,
    probe: Callable[[], Any],
) -> Any:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = probe()
            if value is not None:
                return value
        except (OSError, json.JSONDecodeError) as exc:
            last_error = exc
        time.sleep(0.01)
    detail = f": {last_error}" if last_error else ""
    raise SmokeFailure(f"timed out waiting for {description}{detail}")


class Frontend:
    def __init__(
        self,
        quickshell: str,
        config: pathlib.Path,
        env: dict[str, str],
        log_path: pathlib.Path,
        timeout: float,
    ) -> None:
        self.quickshell = quickshell
        self.config = config
        self.env = env
        self.log_path = log_path
        self.timeout = timeout
        self.process: subprocess.Popen[bytes] | None = None
        self.log_handle: Any = None

    def start(self) -> None:
        self.log_handle = self.log_path.open("wb")
        self.process = subprocess.Popen(
            launch_arguments(self.quickshell, "-p", str(self.config)),
            stdin=subprocess.DEVNULL,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            env=self.env,
        )

        def ready() -> dict[str, Any] | None:
            require(self.process is not None, "frontend process was not created")
            if self.process.poll() is not None:
                raise SmokeFailure(
                    f"Quickshell exited during startup: {self.log_tail()}"
                )
            completed = self._ipc("call", TARGET, "status", check=False)
            if completed.returncode != 0:
                return None
            try:
                return parse_object(decode_return(completed.stdout), "status")
            except SmokeFailure:
                return None

        try:
            wait_for("standalone IPC", self.timeout, ready)
        except SmokeFailure as exc:
            raise SmokeFailure(f"{exc}: {self.log_tail()}") from exc

    def _base(self) -> list[str]:
        require(self.process is not None, "frontend is not running")
        return [self.quickshell, "ipc", "--pid", str(self.process.pid)]

    def _ipc(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            [*self._base(), *arguments],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=self.timeout,
            check=False,
        )
        if check and completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise SmokeFailure(f"IPC {' '.join(arguments)} failed: {detail}")
        return completed

    def call(self, function: str, *arguments: str) -> str:
        completed = self._ipc("call", TARGET, function, *arguments)
        return decode_return(completed.stdout)

    def status(self) -> dict[str, Any]:
        return parse_object(self.call("status"), "status")

    def action(self, action: str, argument: str = "") -> str:
        return self.call("testAction", action, argument)

    def log_tail(self) -> str:
        if self.log_handle is not None:
            self.log_handle.flush()
        if not self.log_path.exists():
            return "no log"
        return self.log_path.read_text(encoding="utf-8", errors="replace")[-1600:].strip()

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        if self.log_handle is not None:
            self.log_handle.close()
            self.log_handle = None


def mock_response(
    correlation_id: str,
    sequence: int,
    query: str,
) -> dict[str, Any]:
    return {
        "request_id": correlation_id,
        "sequence": sequence,
        "query": query,
        "default_mode": "han",
        "available_modes": ["han", "en"],
        "unavailable_modes": ["ja"],
        "segmented": True,
        # Deliberately incorrect legacy data proves canonical token_items wins.
        "tokens": ["legacy-must-not-win"],
        "token_items": [
            {"key": "0:同", "index": 0, "token": "同"},
            {"key": "1:同", "index": 1, "token": "同"},
            {"key": "2:空", "index": 2, "token": "空"},
        ],
        "results_by_mode": {
            "han": {
                "body": "exact-han-preview",
                "full_body": "exact-han-full",
                "full_chars": 14,
            },
            "en": {
                "body": "exact-en-preview",
                "full_body": None,
                "full_ref": "exact-en-ref",
                "full_chars": 12000,
                "truncated": True,
            },
        },
        "related_by_mode": {
            "en": {
                "body": "related-en-preview",
                "full_body": None,
                "full_ref": "related-en-ref",
                "full_chars": 13000,
                "truncated": True,
            }
        },
        "results_by_token": {
            "0:同": {
                "token": "同",
                "index": 0,
                "default_mode": "han",
                "available_modes": ["han"],
                "results_by_mode": {
                    "han": {
                        "body": "first-duplicate-preview",
                        "full_body": None,
                        "full_ref": "first-duplicate-ref",
                        "full_chars": 14000,
                        "truncated": True,
                    }
                },
            },
            "1:同": {
                "token": "同",
                "index": 1,
                "default_mode": "han",
                "available_modes": ["han"],
                "results_by_mode": {
                    "han": {
                        "body": "second-duplicate-preview",
                        "full_body": None,
                        "full_ref": "second-duplicate-ref",
                        "full_chars": 15000,
                        "truncated": True,
                    }
                },
            },
            "2:空": {
                "token": "空",
                "index": 2,
                "default_mode": "han",
                "available_modes": [],
                "results_by_mode": {
                    "han": {"body": "must-remain-unselectable"}
                },
            },
        },
    }


def segmented_miss_response(
    correlation_id: str | None = None,
    sequence: int | None = None,
    query: str | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "default_mode": None,
        "available_modes": [],
        "segmented": True,
        "incomplete_modes": ["han"],
        "incomplete_tokens": ["0:空"],
        "token_items": [
            {"key": "0:空", "index": 0, "token": "空"},
            {"key": "1:首", "index": 1, "token": "首"},
            {"key": "2:次", "index": 2, "token": "次"},
        ],
        "results_by_mode": {},
        "related_by_mode": {},
        "results_by_token": {
            "0:空": {
                "token": "空",
                "index": 0,
                "available_modes": [],
                "results_by_mode": {"han": {"body": "must-not-be-selected"}},
            },
            "1:首": {
                "token": "首",
                "index": 1,
                "default_mode": "han",
                "available_modes": ["han"],
                "results_by_mode": {
                    "han": {
                        "body": "first-usable-preview",
                        "full_body": None,
                        "full_ref": "first-usable-ref",
                        "full_chars": 16000,
                        "truncated": True,
                    }
                },
            },
            "2:次": {
                "token": "次",
                "index": 2,
                "default_mode": "han",
                "available_modes": ["han", "en"],
                "results_by_mode": {
                    "han": {"body": "second-han-preview"},
                    "en": {"body": "second-en-preview"},
                },
            },
        },
    }
    if correlation_id is not None:
        response["request_id"] = correlation_id
    if sequence is not None:
        response["sequence"] = sequence
    if query is not None:
        response["query"] = query
    return response


def wait_text(path: pathlib.Path, expected: str, timeout: float) -> str:
    def probe() -> str | None:
        if not path.exists():
            return None
        value = path.read_text(encoding="utf-8")
        return value if value == expected else None

    return wait_for(f"{path.name}={expected!r}", timeout, probe)


def wait_open(path: pathlib.Path, timeout: float) -> dict[str, Any]:
    def probe() -> dict[str, Any] | None:
        if not path.exists():
            return None
        argv_path = path.with_name(path.name + ".argv")
        stdin_path = path.with_name(path.name + ".stdin")
        if not argv_path.exists() or not stdin_path.exists():
            return None
        argv = [
            item.decode("utf-8")
            for item in argv_path.read_bytes().rstrip(b"\0").split(b"\0")
            if item
        ]
        return {
            "argv": argv,
            "stdin": stdin_path.read_text(encoding="utf-8"),
        }

    return wait_for(path.name, timeout, probe)


def assert_ref_open(
    capture: dict[str, Any],
    expected_ref: str,
    expected_query: str,
) -> None:
    argv = capture.get("argv")
    require(isinstance(argv, list), "Open capture argv is not a list")
    require(argv[:1] == ["open-ref"], f"Open did not use open-ref: {argv}")
    require("--ref" in argv, f"Open ref flag missing: {argv}")
    ref_index = argv.index("--ref")
    require(ref_index + 1 < len(argv) and argv[ref_index + 1] == expected_ref,
            f"Open selected wrong ref: {argv}")
    require(expected_query not in argv, "query leaked into Open process argv")
    require(capture.get("stdin") == expected_query, "Open did not send query on stdin")


def assert_inline_open(capture: dict[str, Any], expected_body: str) -> None:
    argv = capture.get("argv")
    require(isinstance(argv, list), "inline Open capture argv is not a list")
    require(argv[:1] == ["open-text"], f"inline Open did not use open-text: {argv}")
    require(expected_body not in argv, "full body leaked into inline Open process argv")
    require(capture.get("stdin") == expected_body,
            "inline Open did not send the complete body on stdin")


def run_smoke(timeout: float) -> None:
    assert_no_standalone_qs_imports()
    quickshell = shutil.which("quickshell")
    require(quickshell is not None, "quickshell is not installed")

    with tempfile.TemporaryDirectory(prefix="dank-lookup-qml-smoke-") as temporary:
        temp = pathlib.Path(temporary)
        app = temp / "app"
        app.mkdir(mode=0o700)
        shutil.copy2(SHELL, app / "shell.qml")
        shutil.copytree(QML_ROOT, app / "qml")

        home = temp / "home"
        runtime = temp / "runtime"
        bindir = temp / "bin"
        home.mkdir(mode=0o700)
        runtime.mkdir(mode=0o700)
        bindir.mkdir(mode=0o700)
        backend, clipboard, selection = write_fake_processes(bindir)

        copy_capture = temp / "copy.txt"
        open_capture = temp / "open.json"
        transport_capture = temp / "transport.json"
        backend_response = temp / "backend-response.json"
        empty_selection_flag = temp / "empty-selection"
        invalid_theme = temp / "invalid-theme.json"
        invalid_theme.write_text('{"primary":"not-a-colour"}\n', encoding="utf-8")

        env = offscreen_environment(os.environ)
        env.update(
            {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_DATA_HOME": str(home / ".local/share"),
                "XDG_CACHE_HOME": str(home / ".cache"),
                "XDG_RUNTIME_DIR": str(runtime),
                "DANK_LOOKUP_ENABLE_MOCK_IPC": "1",
                "DANK_LOOKUP_BENCHMARK": "1",
                "DANK_LOOKUP_THEME_VARIANT": "dark",
                "DANK_LOOKUP_THEME_FILE": str(invalid_theme),
                "DANK_LOOKUP_COMMAND": str(backend),
                "DANK_LOOKUP_SELECTION_COMMAND": str(selection),
                "DANK_LOOKUP_WL_COPY": str(clipboard),
                "DANK_LOOKUP_SMOKE_COPY_CAPTURE": str(copy_capture),
                "DANK_LOOKUP_SMOKE_OPEN_CAPTURE": str(open_capture),
                "DANK_LOOKUP_SMOKE_TRANSPORT_CAPTURE": str(transport_capture),
                "DANK_LOOKUP_SMOKE_BACKEND_RESPONSE": str(backend_response),
                "DANK_LOOKUP_SMOKE_SELECTION": "capture-private-query",
                "DANK_LOOKUP_SMOKE_EMPTY_SELECTION_FLAG": str(empty_selection_flag),
                "NO_COLOR": "1",
            }
        )

        frontend = Frontend(quickshell, app, env, temp / "quickshell.log", timeout)
        private_query = "private-smoke-query"
        try:
            frontend.start()

            theme = parse_object(frontend.action("theme"), "theme")
            require(theme == {
                "primary": "#c9b8ff",
                "on_primary": "#2f1767",
                "surface": "#121116",
                "surface_container_high": "#2b292f",
                "on_surface": "#e8e0e9",
                "outline": "#958e99",
                "dark": True,
            }, f"invalid matugen palette did not retain dark fallback: {theme}")

            capture_id = frontend.call("prepare", "selection")
            capture_sequence = int(frontend.status().get("sequence", -1))
            require(frontend.call("capturePrepared", capture_id, "selection") == capture_id,
                    "initial selection capture was not accepted")

            def initial_transport() -> dict[str, Any] | None:
                if not transport_capture.exists():
                    return None
                return json.loads(
                    transport_capture.with_suffix(".json.stdin").read_text(encoding="utf-8")
                )

            initial_envelope = wait_for(
                "initial selection transport", timeout, initial_transport
            )
            require(initial_envelope.get("action") == "initial_selection",
                    f"initial selection used wrong action: {initial_envelope}")
            require(initial_envelope.get("request_id") == capture_id
                    and initial_envelope.get("sequence") == capture_sequence,
                    "initial selection lost UUID/sequence correlation")
            require(initial_envelope.get("query") == "capture-private-query",
                    "captured query did not reach the private transport")
            transport_argv = transport_capture.with_suffix(".json.argv").read_bytes()
            require(b"capture-private-query" not in transport_argv,
                    "captured query appeared in backend client argv")
            wait_for(
                "initial selection completion",
                timeout,
                lambda: frontend.status()
                if frontend.status().get("phase") == "ready" else None,
            )

            first_id = frontend.call("prepare", "selection")
            first_status = frontend.status()
            require(first_status.get("phase") == "loading", "prepare did not enter loading")
            require(first_status.get("visible") is True, "prepare did not open popup immediately")

            second_id = frontend.call("prepare", "selection")
            require(second_id and second_id != first_id, "prepare did not rotate correlation id")
            second_sequence = int(frontend.status().get("sequence", -1))
            stale_response = {
                "request_id": first_id,
                "sequence": second_sequence - 1,
                "default_mode": "han",
                "available_modes": ["han"],
                "results_by_mode": {"han": {"body": "stale-must-not-render"}},
            }
            # Exercise finishLookup's two correlation guards independently,
            # not only capturePrepared's early stale rejection.
            wrong_sequence = {
                "sequence": second_sequence - 1,
                "request_id": second_id,
                "response": stale_response,
            }
            wrong_id = {
                "sequence": second_sequence,
                "request_id": first_id,
                "response": stale_response,
            }
            for fixture in (wrong_sequence, wrong_id):
                require(frontend.action(
                    "finish-fixture", json.dumps(fixture, separators=(",", ":"))
                ) == "ok", "stale completion fixture was rejected")
            stale = frontend.call("capturePrepared", first_id, "selection")
            require(stale == "stale", f"old correlation id was not rejected: {stale}")
            loading = frontend.status()
            require(loading.get("phase") == "loading" and loading.get("visible") is True,
                    f"stale completion changed popup state: {loading}")

            injected = frontend.call(
                "mockResult",
                json.dumps(
                    mock_response(second_id, second_sequence, private_query),
                    ensure_ascii=False,
                ),
            )
            require(injected == "ok", f"mock result was rejected: {injected}")
            ready = frontend.status()
            require(ready.get("phase") == "ready" and ready.get("visible") is True,
                    f"mock result did not render ready popup: {ready}")
            stale_both = {
                "sequence": second_sequence - 1,
                "request_id": first_id,
                "response": stale_response,
            }
            require(frontend.action(
                "finish-fixture", json.dumps(stale_both, separators=(",", ":"))
            ) == "ok", "ready-state stale completion fixture was rejected")
            still_ready = parse_object(frontend.action("snapshot"), "post-stale snapshot")
            require(still_ready.get("phase") == "ready"
                    and still_ready.get("body") == "exact-han-preview",
                    f"stale finish mutated current ready result: {still_ready}")

            inline_spec = parse_object(frontend.action("open-spec"), "inline Open spec")
            require(inline_spec.get("kind") == "inline"
                    and inline_spec.get("stdin_chars") == len("exact-han-full"),
                    f"exact inline Open spec is wrong: {inline_spec}")
            open_capture.unlink(missing_ok=True)
            require(frontend.action("open") == "started", "inline Open did not start")
            assert_inline_open(wait_open(open_capture, timeout), "exact-han-full")

            require(frontend.action("mode", "en") == "ok", "en mode was unavailable")
            require(frontend.action("related", "true") == "ok", "related toggle failed")
            related = parse_object(frontend.action("snapshot"), "related snapshot")
            require(related.get("related") is True, "related result is not active")
            require(related.get("body") == "related-en-preview", "wrong related preview")
            require(related.get("full_ref") == "related-en-ref", "wrong related full ref")
            require(frontend.action("copy-payload") == "related-en-preview",
                    "Copy payload did not follow active related result")

            copy_capture.unlink(missing_ok=True)
            require(frontend.action("copy") == "started", "Copy action did not start")
            wait_text(copy_capture, "related-en-preview", timeout)

            open_capture.unlink(missing_ok=True)
            require(frontend.action("open") == "started", "Open action did not start")
            assert_ref_open(wait_open(open_capture, timeout), "related-en-ref", private_query)

            require(frontend.action("token-item", "1") == "ok",
                    "second duplicate token item was unavailable")
            token = parse_object(frontend.action("snapshot"), "token snapshot")
            require(token.get("token") == "同" and token.get("token_key") == "1:同",
                    f"duplicate token identity was lost: {token}")
            require(token.get("body") == "second-duplicate-preview",
                    "indexed duplicate selected the wrong preview")
            require(token.get("full_ref") == "second-duplicate-ref",
                    "indexed duplicate selected the wrong full ref")

            copy_capture.unlink(missing_ok=True)
            require(frontend.action("copy") == "started", "token Copy did not start")
            wait_text(copy_capture, "second-duplicate-preview", timeout)
            open_capture.unlink(missing_ok=True)
            require(frontend.action("open") == "started", "token Open did not start")
            assert_ref_open(
                wait_open(open_capture, timeout), "second-duplicate-ref", private_query
            )
            require(frontend.action("token-item", "2") == "unavailable",
                    "empty-mode token entry became selectable from key existence alone")

            transport_capture.unlink(missing_ok=True)
            mode_request_id = frontend.call("changeMode", "han")
            require(mode_request_id != "unavailable", "mode change was not transported")

            def mode_transport() -> dict[str, Any] | None:
                if not transport_capture.exists():
                    return None
                return json.loads(
                    transport_capture.with_suffix(".json.stdin").read_text(encoding="utf-8")
                )

            mode_envelope = wait_for("mode change transport", timeout, mode_transport)
            require(mode_envelope.get("action") == "mode_change"
                    and mode_envelope.get("mode") == "han",
                    f"mode change envelope is wrong: {mode_envelope}")
            require(mode_envelope.get("query") == private_query,
                    "mode change did not reuse the in-memory query")
            wait_for(
                "mode change completion",
                timeout,
                lambda: frontend.status()
                if frontend.status().get("phase") == "ready" else None,
            )

            transport_capture.unlink(missing_ok=True)
            source_request_id = frontend.call("changeSource", "clipboard")
            require(source_request_id != "unavailable", "source change was not transported")
            source_envelope = wait_for("source change transport", timeout, mode_transport)
            require(source_envelope.get("action") == "source_change"
                    and source_envelope.get("source") == "clipboard",
                    f"source change envelope is wrong: {source_envelope}")
            wait_for(
                "source change completion",
                timeout,
                lambda: frontend.status()
                if frontend.status().get("phase") == "ready" else None,
            )

            transport_capture.unlink(missing_ok=True)
            relookup_id = frontend.call("relookup", "clipboard", "han")
            require(relookup_id != "unavailable", "relookup was not transported")
            relookup_envelope = wait_for("relookup transport", timeout, mode_transport)
            require(relookup_envelope.get("action") == "relookup",
                    f"relookup envelope is wrong: {relookup_envelope}")

            wait_for(
                "relookup completion",
                timeout,
                lambda: frontend.status()
                if frontend.status().get("phase") == "ready" else None,
            )

            require(frontend.call(
                "mockResult",
                json.dumps(
                    segmented_miss_response(query=private_query),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ) == "ok", "segmented miss mock was rejected")
            automatic = parse_object(
                frontend.action("snapshot"), "automatic token snapshot"
            )
            require(automatic.get("token_key") == "1:首"
                    and automatic.get("body") == "first-usable-preview",
                    f"first usable token was not selected: {automatic}")
            require(automatic.get("incomplete_label") == "Timed out: 空",
                    f"token cut short by the budget was not named: {automatic}")
            require(frontend.action("copy-payload") == "first-usable-preview",
                    "automatic token Copy payload used the Whole result")
            open_capture.unlink(missing_ok=True)
            require(frontend.action("open") == "started",
                    "automatic token Open did not start")
            assert_ref_open(
                wait_open(open_capture, timeout), "first-usable-ref", private_query
            )

            require(frontend.action("token", "") == "ok",
                    "Whole result could not be selected")
            whole = parse_object(frontend.action("snapshot"), "Whole miss snapshot")
            require(whole.get("body") == ""
                    and whole.get("empty_message")
                    == "Whole phrase not found — choose a token",
                    f"Whole miss message is wrong: {whole}")

            require(frontend.action("token-item", "2") == "ok",
                    "second usable token was unavailable")
            require(frontend.action("mode", "en") == "ok",
                    "token English mode was unavailable")
            backend_response.write_text(
                json.dumps(segmented_miss_response(), ensure_ascii=False),
                encoding="utf-8",
            )
            transport_capture.unlink(missing_ok=True)
            follow_id = frontend.call("changeSource", "selection")
            require(follow_id != "unavailable",
                    "active-token source change was not transported")
            follow_envelope = wait_for(
                "active-token source transport", timeout, mode_transport
            )
            require(follow_envelope.get("action") == "source_change"
                    and follow_envelope.get("mode") == "en",
                    f"active-token source envelope is wrong: {follow_envelope}")

            def followed_token() -> dict[str, Any] | None:
                snapshot = parse_object(
                    frontend.action("snapshot"), "followed-token snapshot"
                )
                return snapshot if snapshot.get("token_key") == "2:次" else None

            followed = wait_for("active token after source change", timeout, followed_token)
            require(followed.get("mode") == "en"
                    and followed.get("body") == "second-en-preview",
                    f"source change did not preserve active token/mode: {followed}")
            backend_response.unlink()

            follow_sequence = int(frontend.status().get("sequence", -1))
            require(frontend.action(
                "finish-fixture",
                json.dumps(
                    {
                        "sequence": follow_sequence - 1,
                        "request_id": "wrong-request",
                        "response": segmented_miss_response(
                            "wrong-request", follow_sequence - 1, private_query
                        ),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ) == "ok", "stale active-token fixture was rejected")
            after_stale = parse_object(
                frontend.action("snapshot"), "post-token-stale snapshot"
            )
            require(after_stale.get("token_key") == "2:次"
                    and after_stale.get("body") == "second-en-preview",
                    f"stale response changed active token: {after_stale}")

            no_usable = segmented_miss_response(query=private_query)
            no_usable["results_by_token"]["1:首"]["available_modes"] = []
            no_usable["results_by_token"]["2:次"]["available_modes"] = []
            require(frontend.call(
                "mockResult",
                json.dumps(no_usable, ensure_ascii=False, separators=(",", ":")),
            ) == "ok", "no-usable-token fixture was rejected")
            no_token = parse_object(
                frontend.action("snapshot"), "no usable token snapshot"
            )
            require(no_token.get("token_key") == ""
                    and no_token.get("empty_message")
                    == "Whole phrase not found — choose a token",
                    f"no-usable-token Whole state is wrong: {no_token}")

            transport_capture.unlink(missing_ok=True)
            empty_selection_flag.touch(mode=0o600)
            empty_id = frontend.call("prepare", "selection")
            require(frontend.call("capturePrepared", empty_id, "selection") == empty_id,
                    "empty selection capture was not accepted")

            def empty_status() -> dict[str, Any] | None:
                status = frontend.status()
                return status if status.get("phase") == "error" else None

            empty = wait_for("metadata-only empty selection", timeout, empty_status)
            require(empty.get("selection_status") == "success_empty",
                    f"empty selection status was lost: {empty}")
            require(not transport_capture.exists(),
                    "empty selection incorrectly created a query transport")
            empty_selection_flag.unlink()

            error_id = frontend.call("prepare", "selection")
            error_sequence = int(frontend.status().get("sequence", -1))
            error_payload = {
                "request_id": error_id,
                "sequence": error_sequence,
                "error": {
                    "code": "SMOKE_STRUCTURED",
                    "message": "Readable structured smoke error",
                },
            }
            require(frontend.call(
                "mockResult", json.dumps(error_payload, separators=(",", ":"))
            ) == "ok", "structured error mock was rejected")
            error = parse_object(frontend.action("snapshot"), "error snapshot")
            require(error.get("phase") == "error", "structured error did not set error phase")
            require(error.get("error") == "Readable structured smoke error",
                    f"structured error message was not rendered: {error}")

            timing_id = frontend.call("prepare", "benchmark")
            require(frontend.call(
                "capturePrepared", timing_id, "benchmark"
            ) == timing_id, "timing probe was not accepted")
            benchmark = parse_object(frontend.call("benchmarkState"), "benchmark state")
            lookup_started_ns = benchmark.get("lookup_started_ns")
            require(isinstance(lookup_started_ns, (int, float))
                    and lookup_started_ns >= 0,
                    "benchmark state omits accepted lookup monotonic timestamp")
        finally:
            frontend.stop()

        log_text = (temp / "quickshell.log").read_text(
            encoding="utf-8", errors="replace"
        )
        require(private_query not in log_text, "private query appeared in frontend log")
        require("capture-private-query" not in log_text,
                "captured query appeared in frontend log")
        runtime_errors = re.findall(
            r"(?:TypeError|ReferenceError|QQmlApplicationEngine failed|Unable to assign)",
            log_text,
        )
        require(not runtime_errors,
                "QML runtime errors detected: " + ", ".join(sorted(set(runtime_errors))))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TEST_DEADLINE_SECONDS)
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        run_smoke(args.timeout)
    except Exception as exc:  # exact one-line contract for CI and reviewers
        print(f"QML_SMOKE: FAIL: {exc}", file=sys.stderr)
        return 1
    print("QML_SMOKE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
