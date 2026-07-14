#!/usr/bin/env python3
"""Evidence gate for the fixed private user-service socket candidate.

The service mode is a test-only prototype. It uses the proposed bounded,
length-prefixed envelope and is launched with metadata-only argv. The gate
selects the fixed socket only when lifecycle, privacy, cancellation, peer UID,
concurrency, and stale-sequence contracts all pass.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import pathlib
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from typing import Any, Callable


ROOT = pathlib.Path(__file__).resolve().parent.parent
MAX_FRAME_BYTES = 1024 * 1024
HEADER = struct.Struct(">I")
PHASES = ("sdcv_running", "tokenizer_running", "response_queued")


class TransportError(RuntimeError):
    pass


def runtime_root(base: pathlib.Path) -> pathlib.Path:
    return base / "dank-lookup"


def socket_path(base: pathlib.Path) -> pathlib.Path:
    return runtime_root(base) / "backend.sock"


def encode_frame(value: dict[str, Any]) -> bytes:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_FRAME_BYTES:
        raise TransportError("frame_too_large")
    return HEADER.pack(len(payload)) + payload


def recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        part = connection.recv(size - len(chunks))
        if not part:
            raise TransportError("unexpected_eof")
        chunks.extend(part)
    return bytes(chunks)


def recv_frame(connection: socket.socket) -> dict[str, Any]:
    length = HEADER.unpack(recv_exact(connection, HEADER.size))[0]
    if length < 2 or length > MAX_FRAME_BYTES:
        raise TransportError("invalid_frame_length")
    try:
        value = json.loads(recv_exact(connection, length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TransportError("invalid_frame_json") from exc
    if not isinstance(value, dict):
        raise TransportError("invalid_frame_shape")
    return value


def send_frame(connection: socket.socket, value: dict[str, Any]) -> None:
    connection.sendall(encode_frame(value))


def peer_uid(connection: socket.socket) -> int:
    credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    _pid, uid, _gid = struct.unpack("3i", credentials)
    return uid


class FixedSocketService:
    def __init__(self, base: pathlib.Path, idle_timeout: float, expected_uid: int) -> None:
        self.base = base
        self.root = runtime_root(base)
        self.path = socket_path(base)
        self.idle_timeout = idle_timeout
        self.expected_uid = expected_uid
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.last_activity = time.monotonic()
        self.active: dict[tuple[str, int], dict[str, Any]] = {}
        self.cancel_events: dict[tuple[str, int], threading.Event] = {}
        self.observed_peer_uids: set[int] = set()
        self.workers: set[threading.Thread] = set()

    def prepare_path(self) -> None:
        self.root.mkdir(mode=0o700, parents=False, exist_ok=True)
        root_info = self.root.lstat()
        if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.getuid():
            raise TransportError("unsafe_runtime_root")
        if stat.S_IMODE(root_info.st_mode) != 0o700:
            self.root.chmod(0o700)
        if self.path.exists() or self.path.is_symlink():
            info = self.path.lstat()
            if info.st_uid != os.getuid() or not stat.S_ISSOCK(info.st_mode):
                raise TransportError("unsafe_stale_socket")
            self.path.unlink()

    def status(self) -> dict[str, Any]:
        with self.lock:
            active = [
                {"request_id": key[0], "sequence": key[1], "phase": value["phase"]}
                for key, value in sorted(self.active.items())
            ]
            peers = sorted(self.observed_peer_uids)
        return {"status": "ok", "active": active, "peer_uids": peers}

    def _lookup(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = str(request.get("request_id", ""))
        sequence = int(request.get("sequence", -1))
        key = (request_id, sequence)
        cancel_event = threading.Event()
        with self.lock:
            self.cancel_events[key] = cancel_event
            self.active[key] = {"phase": "accepted"}
        delays = request.get("test_delays_ms", {})
        if not isinstance(delays, dict):
            delays = {}
        try:
            for phase in PHASES:
                with self.lock:
                    self.active[key]["phase"] = phase
                delay = max(0.0, min(1.0, float(delays.get(phase, 0)) / 1000))
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline:
                    if cancel_event.wait(min(0.01, max(0.0, deadline - time.monotonic()))):
                        return {
                            "request_id": request_id,
                            "sequence": sequence,
                            "status": "cancelled",
                            "phase": phase,
                        }
                if cancel_event.is_set():
                    return {
                        "request_id": request_id,
                        "sequence": sequence,
                        "status": "cancelled",
                        "phase": phase,
                    }
            return {
                "request_id": request_id,
                "sequence": sequence,
                "status": "ok",
                "action": str(request.get("action", "")),
                "source": request.get("source"),
                "mode": request.get("mode"),
                "query": str(request.get("query", "")),
            }
        finally:
            with self.lock:
                self.active.pop(key, None)
                self.cancel_events.pop(key, None)
                self.last_activity = time.monotonic()

    def handle(self, connection: socket.socket) -> None:
        shutdown_after_response = False
        try:
            uid = peer_uid(connection)
            with self.lock:
                self.observed_peer_uids.add(uid)
            if uid != self.expected_uid:
                send_frame(connection, {"error": {"code": "peer_uid_mismatch"}})
                return
            request = recv_frame(connection)
            action = str(request.get("action", ""))
            if action == "status":
                response = self.status()
            elif action == "cancel":
                key = (str(request.get("request_id", "")), int(request.get("sequence", -1)))
                with self.lock:
                    event = self.cancel_events.get(key)
                if event is not None:
                    event.set()
                response = {"status": "cancel_ack", "matched": event is not None}
            elif action == "shutdown":
                response = {"status": "shutdown_ack"}
                shutdown_after_response = True
            elif action in {"initial_selection", "source_change", "mode_change", "relookup", "lookup"}:
                response = self._lookup(request)
            else:
                response = {"error": {"code": "invalid_action"}}
            send_frame(connection, response)
            if shutdown_after_response:
                self.stop_event.set()
        except (OSError, ValueError, TypeError, TransportError):
            try:
                send_frame(connection, {"error": {"code": "invalid_request"}})
            except OSError:
                pass
        finally:
            connection.close()
            with self.lock:
                self.last_activity = time.monotonic()
                self.workers.discard(threading.current_thread())

    def serve(self) -> int:
        self.prepare_path()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self.path))
            self.path.chmod(0o600)
            server.listen(32)
            server.settimeout(0.02)
            while not self.stop_event.is_set():
                try:
                    connection, _address = server.accept()
                except socket.timeout:
                    with self.lock:
                        idle = not self.active and time.monotonic() - self.last_activity >= self.idle_timeout
                    if idle:
                        break
                    continue
                with self.lock:
                    self.last_activity = time.monotonic()
                worker = threading.Thread(target=self.handle, args=(connection,), daemon=True)
                with self.lock:
                    self.workers.add(worker)
                worker.start()
        finally:
            server.close()
            with self.lock:
                workers = list(self.workers)
            for worker in workers:
                worker.join(timeout=2.0)
            try:
                if self.path.is_socket() and self.path.lstat().st_uid == os.getuid():
                    self.path.unlink()
            except OSError:
                pass
        return 0


def request(path: pathlib.Path, envelope: dict[str, Any], timeout: float = 3.0) -> dict[str, Any]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(timeout)
        client.connect(str(path))
        send_frame(client, envelope)
        return recv_frame(client)
    finally:
        client.close()


def wait_for(predicate: Callable[[], bool], timeout: float, label: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {label}")


def wait_for_phase(path: pathlib.Path, request_id: str, sequence: int, phase: str) -> None:
    def observed() -> bool:
        try:
            status = request(path, {"action": "status"}, timeout=0.2)
        except OSError:
            return False
        return any(
            item == {"request_id": request_id, "sequence": sequence, "phase": phase}
            for item in status.get("active", [])
        )

    wait_for(observed, 2.0, f"{request_id}/{sequence} phase {phase}")


def start_service(
    base: pathlib.Path,
    evidence_dir: pathlib.Path,
    label: str,
    *,
    idle_timeout: float = 5.0,
    expected_uid: int | None = None,
) -> tuple[subprocess.Popen[bytes], Any, Any]:
    stdout_handle = (evidence_dir / f"{label}.service.stdout.log").open("wb")
    stderr_handle = (evidence_dir / f"{label}.service.stderr.log").open("wb")
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    process = subprocess.Popen(
        [
            sys.executable,
            str(pathlib.Path(__file__).resolve()),
            "--serve",
            "--runtime-base",
            str(base),
            "--idle-timeout",
            str(idle_timeout),
            "--expected-uid",
            str(os.getuid() if expected_uid is None else expected_uid),
        ],
        stdin=subprocess.DEVNULL,
        stdout=stdout_handle,
        stderr=stderr_handle,
        env=environment,
        start_new_session=True,
    )

    def ready() -> bool:
        if process.poll() is not None:
            return False
        if not socket_path(base).is_socket():
            return False
        try:
            response = request(socket_path(base), {"action": "status"}, timeout=0.1)
        except (OSError, TransportError):
            return False
        return response.get("status") == "ok" or response == {
            "error": {"code": "peer_uid_mismatch"}
        }

    try:
        wait_for(ready, 3.0, f"{label} service socket")
    except BaseException:
        process.kill()
        process.wait(timeout=1.0)
        stdout_handle.close()
        stderr_handle.close()
        raise
    return process, stdout_handle, stderr_handle


def stop_service(
    process: subprocess.Popen[bytes],
    path: pathlib.Path,
    stdout_handle: Any,
    stderr_handle: Any,
) -> None:
    if process.poll() is None:
        try:
            request(path, {"action": "shutdown"}, timeout=1.0)
        except OSError:
            pass
    try:
        process.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1.0)
    stdout_handle.close()
    stderr_handle.close()


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


def run_gate(args: argparse.Namespace) -> int:
    timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    evidence_dir = args.evidence_dir or (
        ROOT / "tests/evidence/socket-architecture" / f"gate-{timestamp}"
    )
    evidence_dir.mkdir(parents=True, mode=0o700)
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

    with tempfile.TemporaryDirectory(prefix="dank-socket-gate-") as temporary:
        base = pathlib.Path(temporary) / "runtime"
        base.mkdir(mode=0o700)
        path = socket_path(base)
        service, stdout_handle, stderr_handle = start_service(base, evidence_dir, "primary")
        try:
            def permissions_case() -> dict[str, Any]:
                root_mode = stat.S_IMODE(runtime_root(base).stat().st_mode)
                socket_mode = stat.S_IMODE(path.lstat().st_mode)
                assert path == base / "dank-lookup/backend.sock"
                assert root_mode == 0o700
                assert socket_mode == 0o600
                status = request(path, {"action": "status"})
                assert status["peer_uids"] == [os.getuid()]
                return {"runtime_mode": oct(root_mode), "socket_mode": oct(socket_mode)}

            check("fixed_path_permissions_peer_uid", permissions_case)

            def cold_and_running() -> dict[str, Any]:
                first = request(
                    path,
                    {
                        "action": "initial_selection",
                        "request_id": str(uuid.uuid4()),
                        "sequence": 1,
                        "query": "cold-query",
                    },
                )
                second = request(
                    path,
                    {
                        "action": "relookup",
                        "request_id": str(uuid.uuid4()),
                        "sequence": 2,
                        "query": "warm-query",
                    },
                )
                assert first["status"] == second["status"] == "ok"
                return {"service_pid": service.pid}

            check("cold_start_and_server_already_running", cold_and_running)

            def concurrency_case() -> dict[str, Any]:
                envelopes = [
                    {
                        "action": "lookup",
                        "request_id": str(uuid.uuid4()),
                        "sequence": index,
                        "query": f"parallel-{index}",
                        "test_delays_ms": {"sdcv_running": 30},
                    }
                    for index in range(5)
                ]
                with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                    responses = list(executor.map(lambda value: request(path, value), envelopes))
                expected = {(item["request_id"], item["sequence"]) for item in envelopes}
                actual = {(item["request_id"], item["sequence"]) for item in responses}
                assert actual == expected
                return {"parallel_requests": len(responses)}

            check("five_parallel_uuid_sequence_pairing", concurrency_case)

            def unified_actions_case() -> dict[str, Any]:
                actions = ("initial_selection", "source_change", "mode_change", "relookup")
                responses = []
                for sequence, action in enumerate(actions, 10):
                    envelope = {
                        "action": action,
                        "request_id": str(uuid.uuid4()),
                        "sequence": sequence,
                        "source": "FixtureSource",
                        "mode": "han",
                        "query": "same-memory-query",
                    }
                    responses.append(request(path, envelope))
                assert [item["action"] for item in responses] == list(actions)
                assert all(item["query"] == "same-memory-query" for item in responses)
                return {"actions": list(actions)}

            check("unified_action_envelope", unified_actions_case)

            for target_phase in PHASES:
                def cancellation_case(phase: str = target_phase) -> dict[str, Any]:
                    request_id = str(uuid.uuid4())
                    sequence = 40 + PHASES.index(phase)
                    envelope = {
                        "action": "lookup",
                        "request_id": request_id,
                        "sequence": sequence,
                        "query": "cancel-query",
                        "test_delays_ms": {phase: 300},
                    }
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                        future = executor.submit(request, path, envelope)
                        wait_for_phase(path, request_id, sequence, phase)
                        cancel = request(
                            path,
                            {"action": "cancel", "request_id": request_id, "sequence": sequence},
                        )
                        response = future.result(timeout=2.0)
                    assert cancel == {"status": "cancel_ack", "matched": True}
                    assert response["status"] == "cancelled"
                    assert response["phase"] == phase
                    return {"phase": phase}

                check(f"cancel_{target_phase}", cancellation_case)

            def stale_case() -> dict[str, Any]:
                request_id = str(uuid.uuid4())
                old = {
                    "action": "lookup",
                    "request_id": request_id,
                    "sequence": 50,
                    "query": "old-query",
                    "test_delays_ms": {"sdcv_running": 250},
                }
                new = {
                    "action": "relookup",
                    "request_id": request_id,
                    "sequence": 51,
                    "query": "new-query",
                }
                tracker = ActiveSequenceTracker()
                tracker.activate(51)
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    old_future = executor.submit(request, path, old)
                    wait_for_phase(path, request_id, 50, "sdcv_running")
                    new_response = request(path, new)
                    assert tracker.apply(new_response)
                    old_response = old_future.result(timeout=2.0)
                assert not tracker.apply(old_response)
                assert tracker.value is not None and tracker.value["query"] == "new-query"
                return {"active_sequence": tracker.active_sequence}

            check("late_old_sequence_rejected", stale_case)

            def privacy_case() -> dict[str, Any]:
                needles = [b"cold-query", b"warm-query", b"parallel-", b"same-memory-query", b"cancel-query"]
                cmdline = pathlib.Path(f"/proc/{service.pid}/cmdline").read_bytes()
                environ = pathlib.Path(f"/proc/{service.pid}/environ").read_bytes()
                assert all(needle not in cmdline for needle in needles)
                assert all(needle not in environ for needle in needles)
                return {"cmdline_bytes": len(cmdline), "environment_bytes": len(environ)}

            check("query_only_in_framed_payload", privacy_case)
        finally:
            stop_service(service, path, stdout_handle, stderr_handle)

        def restart_case() -> dict[str, Any]:
            restarted, out, err = start_service(base, evidence_dir, "restart")
            try:
                response = request(
                    path,
                    {
                        "action": "lookup",
                        "request_id": str(uuid.uuid4()),
                        "sequence": 60,
                        "query": "restart-query",
                    },
                )
                assert response["status"] == "ok"
                return {"restarted_pid": restarted.pid}
            finally:
                stop_service(restarted, path, out, err)

        check("service_restart", restart_case)

        def stale_socket_case() -> dict[str, Any]:
            runtime_root(base).mkdir(mode=0o700, exist_ok=True)
            stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stale.bind(str(path))
            stale.close()
            assert path.is_socket()
            recovered, out, err = start_service(base, evidence_dir, "stale-recovery")
            try:
                response = request(path, {"action": "status"})
                assert response["status"] == "ok"
                return {"recovered_pid": recovered.pid}
            finally:
                stop_service(recovered, path, out, err)

        check("stale_socket_recovery", stale_socket_case)

        def peer_rejection_case() -> dict[str, Any]:
            rejecting, out, err = start_service(
                base,
                evidence_dir,
                "peer-rejection",
                idle_timeout=0.15,
                expected_uid=os.getuid() + 1,
            )
            try:
                response = request(path, {"action": "status"})
                assert response == {"error": {"code": "peer_uid_mismatch"}}
                return {"rejected_uid": os.getuid()}
            finally:
                stop_service(rejecting, path, out, err)

        check("peer_uid_rejection", peer_rejection_case)

        def idle_case() -> dict[str, Any]:
            idle_service, out, err = start_service(
                base, evidence_dir, "idle", idle_timeout=0.05
            )
            try:
                envelope = {
                    "action": "lookup",
                    "request_id": str(uuid.uuid4()),
                    "sequence": 70,
                    "query": "idle-query",
                    "test_delays_ms": {"sdcv_running": 200},
                }
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(request, path, envelope)
                    time.sleep(0.10)
                    assert idle_service.poll() is None
                    response = future.result(timeout=2.0)
                assert response["status"] == "ok"
                idle_service.wait(timeout=1.0)
                assert not path.exists()
                return {"idle_exit_code": idle_service.returncode}
            finally:
                if idle_service.poll() is None:
                    idle_service.kill()
                    idle_service.wait(timeout=1.0)
                out.close()
                err.close()

        check("idle_cleanup_preserves_inflight_request", idle_case)

    failed = [case["name"] for case in cases if case["status"] != "pass"]
    fixed_passed = not failed
    report = {
        "schema_version": 1,
        "kind": "privacy_transport_architecture_gate",
        "fixed_socket": {
            "path_contract": "$XDG_RUNTIME_DIR/dank-lookup/backend.sock",
            "verdict": "pass" if fixed_passed else "fail",
            "failed_cases": failed,
            "cases": cases,
        },
        "per_request_socket": {
            "verdict": "not_run_no_fixed_socket_counterevidence" if fixed_passed else "pending",
            "known_costs": [
                "bind/startup race per request",
                "more stale socket cleanup",
                "separate cancel routing",
                "greater helper/QML coordination surface",
            ],
        },
        "decision": "fixed_private_user_service_socket" if fixed_passed else "blocked",
        "decision_rule": "fixed socket unless reproducible unfixable lifecycle/privacy/cancellation/stale failure",
    }
    report_path = evidence_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    decision_path = evidence_dir / "decision.md"
    decision_path.write_text(
        "# Privacy transport architecture decision\n\n"
        f"- Fixed socket verdict: {'PASS' if fixed_passed else 'FAIL'}\n"
        f"- Selected candidate: {report['decision']}\n"
        "- Fixed path: `$XDG_RUNTIME_DIR/dank-lookup/backend.sock`\n"
        "- Per-request socket: not selected without fixed-socket counterevidence.\n"
        f"- Failed fixed-socket cases: {', '.join(failed) if failed else 'none'}\n",
        encoding="utf-8",
    )
    print(report_path)
    for case in cases:
        print(f"{case['name']}: {case['status']}")
    print(f"SOCKET_ARCHITECTURE_GATE: {'PASS fixed' if fixed_passed else 'FAIL'}")
    return 0 if fixed_passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--runtime-base", type=pathlib.Path)
    parser.add_argument("--idle-timeout", type=float, default=5.0)
    parser.add_argument("--expected-uid", type=int, default=os.getuid())
    parser.add_argument("--evidence-dir", type=pathlib.Path)
    args = parser.parse_args(argv)
    if args.serve:
        if args.runtime_base is None:
            parser.error("--serve requires --runtime-base")
        return FixedSocketService(args.runtime_base, args.idle_timeout, args.expected_uid).serve()
    return run_gate(args)


if __name__ == "__main__":
    raise SystemExit(main())
