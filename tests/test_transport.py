#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import os
import pathlib
import socket
import stat
import struct
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import dank_lookup_runtime as runtime_module  # noqa: E402
from dank_lookup_runtime import (  # noqa: E402
    MAX_REQUEST_FRAME_BYTES,
    MAX_RESPONSE_FRAME_BYTES,
    PrivateBackendServer,
    RuntimeSecurityError,
    backend_socket_path,
    receive_transport_frame,
    request_backend,
)


def envelope(sequence: int, query: str = "fixture", action: str = "lookup") -> dict[str, object]:
    return {
        "action": action,
        "request_id": str(uuid.uuid4()),
        "sequence": sequence,
        "query": query,
    }


class TransportIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.temporary.name}, clear=False)
        self.env.start()
        self.servers: list[tuple[PrivateBackendServer, threading.Thread]] = []

    def tearDown(self):
        for _server, thread in self.servers:
            thread.join(timeout=2.0)
        self.env.stop()
        self.temporary.cleanup()

    def start_server(
        self,
        handler,
        *,
        idle_timeout=0.15,
        expected_uid=None,
        configure_server=None,
    ):
        server = PrivateBackendServer(
            handler,
            idle_timeout=idle_timeout,
            expected_uid=expected_uid,
        )
        if configure_server is not None:
            configure_server(server)
        thread = threading.Thread(target=server.serve, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if backend_socket_path().is_socket():
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    probe.settimeout(0.1)
                    probe.connect(str(backend_socket_path()))
                except OSError:
                    pass
                else:
                    return server, thread
                finally:
                    probe.close()
            if not thread.is_alive():
                break
            time.sleep(0.01)
        self.fail("backend socket did not become ready")

    @staticmethod
    def echo_handler(request, cancel_event):
        if cancel_event.is_set():
            return {"status": "cancelled"}
        return {"status": "ok", "query": request["query"], "action": request["action"]}

    def test_permissions_peer_uid_and_five_parallel_pairs(self):
        _server, _thread = self.start_server(self.echo_handler)
        path = backend_socket_path()
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.lstat().st_mode), 0o600)
        requests = [envelope(index, f"parallel-{index}") for index in range(5)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            responses = list(executor.map(request_backend, requests))
        self.assertEqual(
            {(item["request_id"], item["sequence"]) for item in responses},
            {(item["request_id"], item["sequence"]) for item in requests},
        )

    def test_malformed_frame_and_disconnect_do_not_kill_server(self):
        _server, _thread = self.start_server(self.echo_handler)
        path = backend_socket_path()
        malformed = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        malformed.connect(str(path))
        malformed.sendall(struct.pack(">I", MAX_REQUEST_FRAME_BYTES + 1))
        response = receive_transport_frame(malformed, MAX_RESPONSE_FRAME_BYTES)
        malformed.close()
        self.assertEqual(response, {"error": {"code": "invalid_frame_length"}})

        disconnected = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        disconnected.connect(str(path))
        disconnected.sendall(b"\x00\x00")
        disconnected.close()
        time.sleep(0.02)
        self.assertEqual(request_backend(envelope(2))["status"], "ok")

    def test_cancel_contains_only_identity_and_interrupts_handler(self):
        started = threading.Event()

        def waiting_handler(request, cancel_event):
            started.set()
            cancel_event.wait(1.0)
            return {"status": "late"}

        _server, _thread = self.start_server(waiting_handler)
        lookup_envelope = envelope(3, "private-cancel-query")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(request_backend, lookup_envelope)
            self.assertTrue(started.wait(1.0))
            cancel = request_backend(
                {
                    "action": "cancel",
                    "request_id": lookup_envelope["request_id"],
                    "sequence": lookup_envelope["sequence"],
                }
            )
            response = future.result(timeout=2.0)
        self.assertTrue(cancel["matched"])
        self.assertEqual(response["status"], "cancelled")

    def test_cancel_before_response_commit_wins_atomically(self):
        response_queued = threading.Event()
        allow_commit = threading.Event()

        def configure(server):
            original = server._commit_response

            def gated_commit(key, active, response):
                response_queued.set()
                self.assertTrue(allow_commit.wait(1.0))
                return original(key, active, response)

            server._commit_response = gated_commit

        server, _thread = self.start_server(
            self.echo_handler,
            idle_timeout=0.5,
            configure_server=configure,
        )
        lookup_envelope = envelope(31, "queued-cancel")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(request_backend, lookup_envelope)
            self.assertTrue(response_queued.wait(1.0))
            cancel = request_backend(
                {
                    "action": "cancel",
                    "request_id": lookup_envelope["request_id"],
                    "sequence": lookup_envelope["sequence"],
                }
            )
            allow_commit.set()
            response = future.result(timeout=2.0)
        self.assertTrue(cancel["matched"])
        self.assertEqual(response["status"], "cancelled")
        with server._lock:
            self.assertEqual(server._active, {})

    def test_cancel_after_response_commit_is_unmatched(self):
        send_started = threading.Event()
        allow_send = threading.Event()
        lookup_envelope = envelope(32, "committed-response")
        original_send = runtime_module.send_transport_frame

        def gated_send(connection, value, max_bytes):
            if (
                value.get("request_id") == lookup_envelope["request_id"]
                and value.get("status") == "ok"
            ):
                send_started.set()
                self.assertTrue(allow_send.wait(1.0))
            return original_send(connection, value, max_bytes)

        with mock.patch.object(runtime_module, "send_transport_frame", side_effect=gated_send):
            server, _thread = self.start_server(self.echo_handler, idle_timeout=0.5)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(request_backend, lookup_envelope)
                self.assertTrue(send_started.wait(1.0))
                cancel = request_backend(
                    {
                        "action": "cancel",
                        "request_id": lookup_envelope["request_id"],
                        "sequence": lookup_envelope["sequence"],
                    }
                )
                allow_send.set()
                response = future.result(timeout=2.0)
        self.assertFalse(cancel["matched"])
        self.assertEqual(response["status"], "ok")
        with server._lock:
            self.assertEqual(server._active, {})

    def test_identity_pairing_supports_client_side_stale_rejection(self):
        _server, _thread = self.start_server(self.echo_handler)
        request_id = str(uuid.uuid4())
        old = {"action": "lookup", "request_id": request_id, "sequence": 4, "query": "old"}
        new = {"action": "relookup", "request_id": request_id, "sequence": 5, "query": "new"}
        old_response = request_backend(old)
        new_response = request_backend(new)
        active_sequence = 5
        self.assertNotEqual(old_response["sequence"], active_sequence)
        self.assertEqual(new_response["sequence"], active_sequence)

    def test_peer_uid_mismatch_is_rejected(self):
        _server, _thread = self.start_server(
            self.echo_handler,
            expected_uid=os.getuid() + 1,
        )
        response = request_backend(envelope(6))
        self.assertEqual(response, {"error": {"code": "peer_uid_mismatch"}})

    def test_idle_cleanup_waits_for_inflight_request(self):
        started = threading.Event()

        def slow_handler(request, cancel_event):
            started.set()
            time.sleep(0.2)
            return {"status": "ok"}

        _server, thread = self.start_server(slow_handler, idle_timeout=0.05)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(request_backend, envelope(7))
            self.assertTrue(started.wait(1.0))
            time.sleep(0.08)
            self.assertTrue(thread.is_alive())
            self.assertEqual(future.result(timeout=2.0)["status"], "ok")
        thread.join(timeout=1.0)
        self.assertFalse(thread.is_alive())
        self.assertFalse(backend_socket_path(create=False).exists())

    def test_live_socket_cannot_be_taken_over_or_unlinked_by_previous_server(self):
        def identifying_handler(name):
            def handle(_request, _cancel_event):
                return {"status": "ok", "server": name}

            return handle

        first, first_thread = self.start_server(
            identifying_handler("one"),
            idle_timeout=0.25,
        )
        path = backend_socket_path()
        first_identity = (path.lstat().st_dev, path.lstat().st_ino)
        second = PrivateBackendServer(
            identifying_handler("two"),
            idle_timeout=1.0,
            path=path,
        )
        second_errors: list[BaseException] = []

        def serve_second():
            try:
                second.serve()
            except BaseException as exc:  # captured for assertions from the test thread
                second_errors.append(exc)

        second_thread = threading.Thread(target=serve_second, daemon=True)
        second_thread.start()
        self.servers.append((second, second_thread))

        deadline = time.monotonic() + 1.0
        takeover_identity = first_identity
        while time.monotonic() < deadline:
            if path.is_socket():
                info = path.lstat()
                takeover_identity = (info.st_dev, info.st_ino)
                if takeover_identity != first_identity:
                    break
            if not second_thread.is_alive():
                break
            time.sleep(0.01)

        takeover = takeover_identity != first_identity
        if takeover:
            takeover_response = request_backend(envelope(8), path=path)
            first_thread.join(timeout=1.5)
            first_removed_replacement = not path.exists()
            second_still_alive = second_thread.is_alive()
            first._stop.set()
            second._stop.set()
            first_thread.join(timeout=1.0)
            second_thread.join(timeout=1.0)
            self.fail(
                "second server took over the live socket "
                f"(response_server={takeover_response.get('server')!r}); "
                "the first server later removed the replacement socket "
                f"(removed={first_removed_replacement}, "
                f"second_alive={second_still_alive})"
            )

        self.assertFalse(second_thread.is_alive())
        self.assertEqual(len(second_errors), 1)
        self.assertIsInstance(second_errors[0], RuntimeSecurityError)
        self.assertIn("already running", str(second_errors[0]))
        current_info = path.lstat()
        self.assertEqual((current_info.st_dev, current_info.st_ino), first_identity)
        self.assertEqual(request_backend(envelope(9), path=path)["server"], "one")

        # Exercise shutdown ownership independently: if the bound name is
        # replaced, the previous server must not unlink the replacement.
        path.unlink()
        second_errors.clear()
        second_thread = threading.Thread(target=serve_second, daemon=True)
        second_thread.start()
        self.servers.append((second, second_thread))
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if path.is_socket():
                break
            if not second_thread.is_alive():
                break
            time.sleep(0.01)
        self.assertTrue(path.is_socket(), second_errors)
        replacement_info = path.lstat()
        replacement_identity = (replacement_info.st_dev, replacement_info.st_ino)
        self.assertNotEqual(replacement_identity, first_identity)

        first_thread.join(timeout=1.5)
        self.assertFalse(first_thread.is_alive())
        self.assertTrue(path.is_socket())
        current_info = path.lstat()
        self.assertEqual((current_info.st_dev, current_info.st_ino), replacement_identity)
        self.assertEqual(request_backend(envelope(10), path=path)["server"], "two")

        second._stop.set()
        second_thread.join(timeout=1.0)
        self.assertFalse(second_thread.is_alive())

    def test_owned_stale_socket_is_recovered(self):
        path = backend_socket_path()
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(path))
        stale_identity = (path.lstat().st_dev, path.lstat().st_ino)
        stale.close()

        _server, _thread = self.start_server(self.echo_handler)
        current_info = path.lstat()
        self.assertNotEqual(
            (current_info.st_dev, current_info.st_ino),
            stale_identity,
        )
        self.assertEqual(request_backend(envelope(11), path=path)["status"], "ok")


if __name__ == "__main__":
    unittest.main()
