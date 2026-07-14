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

from dank_lookup_runtime import (  # noqa: E402
    MAX_REQUEST_FRAME_BYTES,
    MAX_RESPONSE_FRAME_BYTES,
    PrivateBackendServer,
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

    def start_server(self, handler, *, idle_timeout=0.15, expected_uid=None):
        server = PrivateBackendServer(
            handler,
            idle_timeout=idle_timeout,
            expected_uid=expected_uid,
        )
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


if __name__ == "__main__":
    unittest.main()
