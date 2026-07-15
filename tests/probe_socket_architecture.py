#!/usr/bin/env python3
"""Production full-stack gate for the fixed private backend socket.

The gate loads the production CLI handler, runs the production
``PrivateBackendServer``, and uses controllable fake sdcv/tokenizer executables
only at the external process boundary. Cancellation is exercised while a real
sdcv lookup is running, while the real tokenizer adapter is running, and while
the server is waiting to commit a completed production response.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import datetime as dt
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import socket
import stat
import sys
import tempfile
import textwrap
import threading
import time
import types
import uuid
from typing import Any, Callable, Iterator, Mapping


ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import dank_lookup_runtime as runtime_module  # noqa: E402
from dank_lookup_runtime import (  # noqa: E402
    PrivateBackendServer,
    RuntimeSecurityError,
    backend_socket_path,
    request_backend,
)


PHASES = ("sdcv_running", "tokenizer_running", "response_queued")
_CLI_MODULE: types.ModuleType | None = None


def wait_for(predicate: Callable[[], bool], timeout: float, label: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {label}")


def load_cli_module() -> types.ModuleType:
    global _CLI_MODULE
    if _CLI_MODULE is not None:
        return _CLI_MODULE
    path = ROOT / "bin/dank-lookup"
    loader = importlib.machinery.SourceFileLoader("dank_lookup_gate_cli", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError("unable to load production CLI module")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    _CLI_MODULE = module
    return module


def write_executable(path: pathlib.Path, body: str) -> pathlib.Path:
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    path.chmod(0o700)
    return path


@contextlib.contextmanager
def patched_environment(values: Mapping[str, str]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class ActiveSequenceTracker:
    def __init__(self) -> None:
        self.active_sequence = -1
        self.value: dict[str, Any] | None = None

    def activate(self, sequence: int) -> None:
        self.active_sequence = sequence

    def apply(self, response: dict[str, Any]) -> bool:
        if int(response.get("sequence", -1)) != self.active_sequence:
            return False
        self.value = response
        return True


class ProductionStack:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.runtime_base = root / "runtime"
        self.runtime_base.mkdir(mode=0o700)
        self.commands = root / "commands"
        self.commands.mkdir(mode=0o700)
        self.sdcv_pid_file = root / "sdcv.pids"
        self.tokenizer_pid_file = root / "tokenizer.pids"
        self.sdcv = self._write_sdcv()
        self.tokenizer = self._write_tokenizer()
        self.config = self._write_config()
        self.server: PrivateBackendServer | None = None
        self.thread: threading.Thread | None = None

    def _write_sdcv(self) -> pathlib.Path:
        return write_executable(
            self.commands / "fake-sdcv",
            """\
            #!/usr/bin/python3
            import json
            import os
            import pathlib
            import subprocess
            import sys
            import time

            if "--list-dicts" in sys.argv[1:]:
                print("Dictionary's name   Word count")
                print("FixtureDict   1")
                raise SystemExit(0)

            print("Enter word or phrase: ", end="", flush=True)
            query = sys.stdin.readline().rstrip("\\n")
            print(query, flush=True)
            mode = os.environ.get("GATE_SDCV_MODE", "empty")
            if mode == "block":
                child = subprocess.Popen(
                    ["/usr/bin/python3", "-c", "import time; time.sleep(60)"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                pathlib.Path(os.environ["GATE_SDCV_PID_FILE"]).write_text(
                    f"{os.getpid()} {child.pid}", encoding="ascii"
                )
                while True:
                    time.sleep(1)

            entries = []
            if mode == "hit" and not query.startswith("/"):
                entries = [{
                    "dict": "FixtureDict",
                    "word": query,
                    "definition": "production full-stack fixture",
                }]
            print(json.dumps(entries, ensure_ascii=False), flush=True)
            print("Enter word or phrase: ", flush=True)
            """,
        )

    def _write_tokenizer(self) -> pathlib.Path:
        return write_executable(
            self.commands / "fake-tokenizer",
            """\
            #!/usr/bin/python3
            import json
            import os
            import pathlib
            import subprocess
            import sys
            import time

            query = sys.stdin.readline().rstrip("\\n")
            if os.environ.get("GATE_TOKENIZER_MODE", "pass") == "block":
                child = subprocess.Popen(
                    ["/usr/bin/python3", "-c", "import time; time.sleep(60)"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                pathlib.Path(os.environ["GATE_TOKENIZER_PID_FILE"]).write_text(
                    f"{os.getpid()} {child.pid}", encoding="ascii"
                )
                while True:
                    time.sleep(1)
            print(json.dumps({"tokens": [query]}, ensure_ascii=False))
            """,
        )

    def _write_config(self) -> pathlib.Path:
        path = self.root / "config.json"
        path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "tokenizer": str(self.tokenizer),
                    "source_timeout_ms": 5000,
                    "lookup_timeout_ms": 8000,
                    "modes": {
                        "han": {
                            "label": "Fixture Han",
                            "enabled": True,
                            "default_for": ["han"],
                            "sources": [
                                {
                                    "name": "FixtureDict",
                                    "when": "han",
                                    "exact_only": True,
                                }
                            ],
                        }
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def environment(self) -> dict[str, str]:
        return {
            "XDG_RUNTIME_DIR": str(self.runtime_base),
            "DANK_LOOKUP_SDCV": str(self.sdcv),
            "DANK_LOOKUP_TOKENIZER": str(self.tokenizer),
            "DANK_LOOKUP_CONFIG": str(self.config),
            "GATE_SDCV_PID_FILE": str(self.sdcv_pid_file),
            "GATE_TOKENIZER_PID_FILE": str(self.tokenizer_pid_file),
            "GATE_SDCV_MODE": "empty",
            "GATE_TOKENIZER_MODE": "pass",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    @property
    def path(self) -> pathlib.Path:
        return backend_socket_path()

    def start(
        self,
        configure: Callable[[PrivateBackendServer], None] | None = None,
        *,
        idle_timeout: float = 10.0,
    ) -> None:
        handler = load_cli_module()._backend_handler(str(self.config))
        self.server = PrivateBackendServer(
            handler,
            idle_timeout=idle_timeout,
            path=self.path,
        )
        if configure is not None:
            configure(self.server)
        self.thread = threading.Thread(target=self.server.serve, daemon=True)
        self.thread.start()

        def ready() -> bool:
            if self.thread is None or not self.thread.is_alive() or not self.path.is_socket():
                return False
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(0.1)
                probe.connect(str(self.path))
                return True
            except OSError:
                return False
            finally:
                probe.close()

        wait_for(ready, 3.0, "production backend socket")

    def stop(self) -> None:
        if self.server is not None:
            self.server._stop.set()
        if self.thread is not None:
            self.thread.join(timeout=3.0)
            if self.thread.is_alive():
                raise AssertionError("production backend server did not stop")
        self.server = None
        self.thread = None

    def request(self, value: Mapping[str, Any], timeout: float = 5.0) -> dict[str, Any]:
        return request_backend(value, timeout=timeout, path=self.path)

    def active_snapshot(self) -> dict[tuple[str, int], dict[str, bool]]:
        assert self.server is not None
        with self.server._lock:
            return {
                key: {
                    "cancelled": active.cancel_event.is_set(),
                    "response_committed": active.response_committed,
                }
                for key, active in self.server._active.items()
            }

    @staticmethod
    def envelope(sequence: int, query: str, request_id: str | None = None) -> dict[str, Any]:
        return {
            "action": "lookup",
            "request_id": request_id or str(uuid.uuid4()),
            "sequence": sequence,
            "query": query,
            "source": "selection",
        }

    @staticmethod
    def cancel_for(value: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "action": "cancel",
            "request_id": value["request_id"],
            "sequence": value["sequence"],
        }


def read_process_tree(path: pathlib.Path) -> list[int]:
    wait_for(lambda: path.is_file() and bool(path.read_text(encoding="ascii").strip()), 3.0, path.name)
    return [int(value) for value in path.read_text(encoding="ascii").split()]


def assert_query_is_payload_only(query: str, pids: list[int]) -> None:
    needle = query.encode("utf-8")
    for pid in pids:
        cmdline = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
        environ = pathlib.Path(f"/proc/{pid}/environ").read_bytes()
        assert needle not in cmdline
        assert needle not in environ


def wait_for_process_tree_exit(pids: list[int]) -> None:
    wait_for(
        lambda: all(not pathlib.Path(f"/proc/{pid}").exists() for pid in pids),
        3.0,
        f"process tree exit {pids}",
    )


def assert_registry_empty(stack: ProductionStack) -> None:
    wait_for(lambda: not stack.active_snapshot(), 2.0, "empty active registry")


def run_process_cancellation_case(stack: ProductionStack, phase: str) -> dict[str, Any]:
    assert phase in {"sdcv_running", "tokenizer_running"}
    pid_file = stack.sdcv_pid_file if phase == "sdcv_running" else stack.tokenizer_pid_file
    pid_file.unlink(missing_ok=True)
    os.environ["GATE_SDCV_MODE"] = "block" if phase == "sdcv_running" else "empty"
    os.environ["GATE_TOKENIZER_MODE"] = "block" if phase == "tokenizer_running" else "pass"
    query = "取消查詢"
    envelope = stack.envelope(100 + PHASES.index(phase), query)
    key = (str(envelope["request_id"]), int(envelope["sequence"]))
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(stack.request, envelope)
        pids = read_process_tree(pid_file)
        wait_for(lambda: key in stack.active_snapshot(), 1.0, f"active {phase}")
        assert_query_is_payload_only(query, pids)
        cancel = stack.request(stack.cancel_for(envelope))
        response = future.result(timeout=5.0)
    assert cancel == {
        "request_id": envelope["request_id"],
        "sequence": envelope["sequence"],
        "status": "cancel_ack",
        "matched": True,
    }
    assert response == {
        "request_id": envelope["request_id"],
        "sequence": envelope["sequence"],
        "status": "cancelled",
    }
    wait_for_process_tree_exit(pids)
    assert_registry_empty(stack)
    return {
        "phase": phase,
        "process_tree_pids": pids,
        "process_tree_reaped": True,
        "active_registry_empty": True,
    }


def run_response_queue_case(stack: ProductionStack) -> dict[str, Any]:
    os.environ["GATE_SDCV_MODE"] = "hit"
    os.environ["GATE_TOKENIZER_MODE"] = "pass"
    request_id = str(uuid.uuid4())
    old = stack.envelope(200, "排隊回應", request_id)
    new = stack.envelope(201, "較新回應", request_id)
    target = (request_id, 200)
    response_queued = threading.Event()
    allow_commit = threading.Event()
    assert stack.server is not None
    original_commit = stack.server._commit_response

    def gated_commit(key, active, response):
        if key == target:
            response_queued.set()
            if not allow_commit.wait(3.0):
                raise AssertionError("response commit barrier timed out")
        return original_commit(key, active, response)

    stack.server._commit_response = gated_commit
    tracker = ActiveSequenceTracker()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            old_future = executor.submit(stack.request, old)
            assert response_queued.wait(3.0)
            tracker.activate(201)
            new_response = stack.request(new)
            assert tracker.apply(new_response)
            cancel = stack.request(stack.cancel_for(old))
            allow_commit.set()
            old_response = old_future.result(timeout=5.0)
    finally:
        allow_commit.set()
        stack.server._commit_response = original_commit
    assert cancel["matched"] is True
    assert old_response["status"] == "cancelled"
    assert not tracker.apply(old_response)
    assert tracker.value is not None and tracker.value["sequence"] == 201
    assert_registry_empty(stack)
    return {
        "phase": "response_queued",
        "cancel_won_before_commit": True,
        "new_sequence_applied": 201,
        "old_sequence_rejected": True,
        "active_registry_empty": True,
    }


def run_post_commit_case(stack: ProductionStack) -> dict[str, Any]:
    os.environ["GATE_SDCV_MODE"] = "hit"
    envelope = stack.envelope(300, "已提交回應")
    key = (str(envelope["request_id"]), int(envelope["sequence"]))
    send_started = threading.Event()
    allow_send = threading.Event()
    original_send = runtime_module.send_transport_frame

    def gated_send(connection, value, max_bytes):
        if (
            value.get("request_id") == envelope["request_id"]
            and value.get("sequence") == envelope["sequence"]
            and "action" not in value
            and value.get("status") != "cancel_ack"
        ):
            send_started.set()
            if not allow_send.wait(3.0):
                raise AssertionError("transport send barrier timed out")
        return original_send(connection, value, max_bytes)

    runtime_module.send_transport_frame = gated_send
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(stack.request, envelope)
            assert send_started.wait(3.0)
            snapshot = stack.active_snapshot()
            assert snapshot[key]["response_committed"] is True
            cancel = stack.request(stack.cancel_for(envelope))
            allow_send.set()
            response = future.result(timeout=5.0)
    finally:
        allow_send.set()
        runtime_module.send_transport_frame = original_send
    assert cancel["matched"] is False
    assert response["sequence"] == envelope["sequence"]
    assert response.get("status") != "cancelled"
    assert_registry_empty(stack)
    return {
        "commit_semantics": "cancel_before_commit_wins_after_commit_unmatched",
        "cancel_matched": False,
        "normal_response_preserved": True,
        "active_registry_empty": True,
    }


def execute_gate(evidence_dir: pathlib.Path) -> dict[str, Any]:
    evidence_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    cases: list[dict[str, Any]] = []

    def check(name: str, operation: Callable[[], dict[str, Any] | None]) -> None:
        started = time.monotonic()
        errors: list[str] = []
        detail: dict[str, Any] = {}
        try:
            detail = operation() or {}
        except BaseException as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        cases.append(
            {
                "name": name,
                "status": "pass" if not errors else "fail",
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
                "errors": errors,
                "detail": detail,
            }
        )

    with tempfile.TemporaryDirectory(prefix="dank-production-socket-gate-") as temporary:
        stack = ProductionStack(pathlib.Path(temporary))
        with patched_environment(stack.environment()):
            stack.start()
            try:
                def permissions_and_live_owner() -> dict[str, Any]:
                    path = stack.path
                    info = path.lstat()
                    identity = (info.st_dev, info.st_ino)
                    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
                    assert stat.S_IMODE(info.st_mode) == 0o600
                    assert stack.server is not None
                    contender = PrivateBackendServer(
                        load_cli_module()._backend_handler(str(stack.config)),
                        path=path,
                    )
                    try:
                        contender.serve()
                    except RuntimeSecurityError as exc:
                        assert "already running" in str(exc)
                    else:
                        raise AssertionError("second production server took over live socket")
                    current = path.lstat()
                    assert (current.st_dev, current.st_ino) == identity
                    return {
                        "runtime_mode": "0o700",
                        "socket_mode": "0o600",
                        "live_owner_preserved": True,
                    }

                check("production_fixed_path_permissions_live_owner", permissions_and_live_owner)
                check(
                    "cancel_sdcv_running_production_full_stack",
                    lambda: run_process_cancellation_case(stack, "sdcv_running"),
                )
                check(
                    "cancel_tokenizer_running_production_full_stack",
                    lambda: run_process_cancellation_case(stack, "tokenizer_running"),
                )
                check(
                    "cancel_response_queued_production_full_stack",
                    lambda: run_response_queue_case(stack),
                )
                check("cancel_after_commit_is_unmatched", lambda: run_post_commit_case(stack))

                def five_parallel() -> dict[str, Any]:
                    os.environ["GATE_SDCV_MODE"] = "hit"
                    envelopes = [stack.envelope(400 + index, f"並行查詢{index}") for index in range(5)]
                    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                        responses = list(executor.map(stack.request, envelopes))
                    expected = {(item["request_id"], item["sequence"]) for item in envelopes}
                    actual = {(item["request_id"], item["sequence"]) for item in responses}
                    assert actual == expected
                    assert_registry_empty(stack)
                    return {"parallel_requests": 5, "active_registry_empty": True}

                check("five_parallel_production_requests", five_parallel)
            finally:
                stack.stop()

            def restart_and_stale_recovery() -> dict[str, Any]:
                assert not stack.path.exists()
                stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                stale.bind(str(stack.path))
                stale_identity = stack.path.lstat().st_ino
                stale.close()
                stack.start(idle_timeout=1.0)
                try:
                    os.environ["GATE_SDCV_MODE"] = "hit"
                    response = stack.request(stack.envelope(500, "重啟查詢"))
                    assert response["sequence"] == 500
                    assert stack.path.lstat().st_ino != stale_identity
                    return {"stale_socket_recovered": True, "restart_request": True}
                finally:
                    stack.stop()

            check("production_restart_and_stale_socket_recovery", restart_and_stale_recovery)

    failed = [case["name"] for case in cases if case["status"] != "pass"]
    fixed_passed = not failed
    report = {
        "schema_version": 2,
        "kind": "privacy_transport_architecture_gate",
        "implementation_under_test": {
            "server": "lib.dank_lookup_runtime.PrivateBackendServer",
            "handler": "bin/dank-lookup::_backend_handler",
            "lookup": "lib.dank_lookup_core.lookup",
            "external_fixtures_only": ["sdcv", "tokenizer"],
        },
        "cancellation_contract": "cancel_before_response_commit_wins; cancel_after_commit_is_unmatched",
        "fixed_socket": {
            "path_contract": "$XDG_RUNTIME_DIR/dank-lookup/backend.sock",
            "verdict": "pass" if fixed_passed else "fail",
            "failed_cases": failed,
            "cases": cases,
        },
        "per_request_socket": {
            "verdict": "not_run_no_fixed_socket_counterevidence" if fixed_passed else "pending"
        },
        "decision": "fixed_private_user_service_socket" if fixed_passed else "blocked",
        "decision_rule": "production full-stack lifecycle and cancellation cases must all pass",
    }
    (evidence_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (evidence_dir / "decision.md").write_text(
        "# Privacy transport architecture decision\n\n"
        f"- Production fixed socket verdict: {'PASS' if fixed_passed else 'FAIL'}\n"
        f"- Selected candidate: {report['decision']}\n"
        "- Cancellation: real server + production handler/lookup + external fake processes.\n"
        f"- Failed cases: {', '.join(failed) if failed else 'none'}\n",
        encoding="utf-8",
    )
    return report


def run_gate(args: argparse.Namespace) -> int:
    timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    evidence_dir = args.evidence_dir or (
        ROOT / "tests/evidence/socket-architecture" / f"gate-{timestamp}"
    )
    report = execute_gate(evidence_dir)
    print(evidence_dir / "report.json")
    for case in report["fixed_socket"]["cases"]:
        print(f"{case['name']}: {case['status']}")
    passed = report["fixed_socket"]["verdict"] == "pass"
    print(f"SOCKET_ARCHITECTURE_GATE: {'PASS fixed production' if passed else 'FAIL'}")
    return 0 if passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=pathlib.Path)
    return run_gate(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
