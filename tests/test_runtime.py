#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import pathlib
import socket
import stat
import struct
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import dank_lookup_runtime as runtime_module  # noqa: E402
from dank_lookup_runtime import (  # noqa: E402
    MAX_REQUEST_FRAME_BYTES,
    RuntimeSecurityError,
    TransportProtocolError,
    atomic_write_bytes,
    backend_socket_path,
    encode_transport_frame,
    receive_transport_frame,
    remove_private_file,
    read_request,
    runtime_dir,
    text_path,
    validate_transport_envelope,
    blob_path,
    cleanup_private_basename_after_ttl,
    schedule_private_cleanup,
    write_full_blobs,
    write_result,
)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.temporary.name}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temporary.cleanup()

    def test_directory_and_file_permissions(self):
        root = runtime_dir()
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        destination = text_path("permissions")
        atomic_write_bytes(destination, b"private")
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        self.assertEqual(backend_socket_path(), root / "backend.sock")

    def test_length_prefixed_frame_round_trip_and_cap(self):
        left, right = socket.socketpair()
        try:
            envelope = {
                "action": "lookup",
                "request_id": "69f53fc1-8a38-43f0-b4ba-c01949b627ce",
                "sequence": 1,
                "query": "stdin-only",
            }
            left.sendall(encode_transport_frame(envelope, MAX_REQUEST_FRAME_BYTES))
            self.assertEqual(receive_transport_frame(right, MAX_REQUEST_FRAME_BYTES), envelope)
            with self.assertRaises(TransportProtocolError):
                encode_transport_frame({"payload": "x" * 100}, 16)
            left.sendall(struct.pack(">I", MAX_REQUEST_FRAME_BYTES + 1))
            with self.assertRaises(TransportProtocolError):
                receive_transport_frame(right, MAX_REQUEST_FRAME_BYTES)
        finally:
            left.close()
            right.close()

    def test_envelope_rejects_invalid_identity_and_query_bearing_cancel(self):
        with self.assertRaisesRegex(TransportProtocolError, "invalid_request_id"):
            validate_transport_envelope(
                {"action": "lookup", "request_id": "not-a-uuid", "sequence": 1, "query": "x"}
            )
        with self.assertRaisesRegex(TransportProtocolError, "cancel_contains_query"):
            validate_transport_envelope(
                {
                    "action": "cancel",
                    "request_id": "69f53fc1-8a38-43f0-b4ba-c01949b627ce",
                    "sequence": 1,
                    "query": "must-not-be-here",
                }
            )

    def test_atomic_write_never_leaves_temporary_file(self):
        destination = text_path("atomic")
        atomic_write_bytes(destination, b"first")
        atomic_write_bytes(destination, b"second")
        self.assertEqual(destination.read_bytes(), b"second")
        self.assertEqual([path for path in runtime_dir().iterdir() if path.name.startswith(".full-atomic")], [])

    def test_request_sequence_prevents_stale_active_overwrite(self):
        newer = {"request_id": "new", "sequence": 2, "query": "not stored in active index"}
        older = {"request_id": "old", "sequence": 1, "query": "older"}
        self.assertTrue(write_result(newer, {"entries": {}}))
        self.assertFalse(write_result(older, {"entries": {}}))
        active = json.loads((runtime_dir() / "active.json").read_text(encoding="utf-8"))
        self.assertEqual(active["request_id"], "new")
        self.assertEqual(active["sequence"], 2)
        self.assertEqual(read_request("old")["result"]["query"], "older")

    def test_symlink_destination_is_rejected(self):
        target = pathlib.Path(self.temporary.name) / "outside"
        target.write_text("outside", encoding="utf-8")
        destination = text_path("link")
        destination.symlink_to(target)
        with self.assertRaises(RuntimeSecurityError):
            atomic_write_bytes(destination, b"replacement")
        self.assertEqual(target.read_text(encoding="utf-8"), "outside")

    def test_unset_xdg_uses_uid_namespaced_private_tmp_fallback(self):
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": ""}, clear=False):
            root = runtime_dir()
            self.assertEqual(root, pathlib.Path("/tmp") / f"dank-lookup-{os.getuid()}")
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            destination = root / "fallback-test.txt"
            atomic_write_bytes(destination, b"private fallback")
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            destination.unlink()
            try:
                root.rmdir()
            except OSError:
                pass

    def test_fallback_full_blob_ttl_removes_file_and_empty_directory(self):
        fallback = pathlib.Path("/tmp") / f"dank-lookup-{os.getuid()}"
        with mock.patch.dict(
            os.environ,
            {"XDG_RUNTIME_DIR": "", "DANK_LOOKUP_FULL_TTL_SECONDS": "0.05"},
            clear=False,
        ):
            write_full_blobs("ttl-test", {"abcdef": "complete"})
            path = blob_path("ttl-test", "abcdef")
            self.assertTrue(path.is_file())
            for _ in range(100):
                if not path.exists():
                    break
                time.sleep(0.01)
            self.assertFalse(path.exists())
            self.assertFalse(fallback.exists())

    def test_cleanup_scheduler_uses_same_cli_with_metadata_only_argv(self):
        destination = text_path("opaque-request")
        atomic_write_bytes(destination, b"private")
        with mock.patch.object(runtime_module.subprocess, "Popen") as popen:
            schedule_private_cleanup(
                destination,
                ttl_seconds=12.5,
                cli_path=ROOT / "bin/dank-lookup",
            )
        argv = popen.call_args.args[0]
        self.assertEqual(
            pathlib.Path(argv[0]).resolve(), (ROOT / "bin/dank-lookup").resolve()
        )
        self.assertEqual(argv[1:4], ["cleanup-file", "--basename", destination.name])
        self.assertNotIn(str(destination), argv)
        self.assertIn("--ttl", argv)
        self.assertIn("--device", argv)
        self.assertIn("--inode", argv)
        self.assertIn("--mtime-ns", argv)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(
            popen.call_args.kwargs["stdin"], runtime_module.subprocess.DEVNULL
        )
        self.assertNotIn(
            "os.fork", pathlib.Path(runtime_module.__file__).read_text(encoding="utf-8")
        )

    def test_cleanup_enforces_age_mode_owner_and_basename(self):
        destination = text_path("guarded")
        atomic_write_bytes(destination, b"private")
        self.assertFalse(remove_private_file(destination, min_age_seconds=60))
        old = time.time() - 120
        os.utime(destination, (old, old), follow_symlinks=False)
        destination.chmod(0o640)
        with self.assertRaises(RuntimeSecurityError):
            remove_private_file(destination, min_age_seconds=60)
        destination.chmod(0o600)
        with (
            mock.patch.object(runtime_module, "runtime_dir", return_value=destination.parent),
            mock.patch.object(runtime_module, "_validate_owned_directory"),
            mock.patch.object(runtime_module.os, "getuid", return_value=os.getuid() + 1),
            self.assertRaises(RuntimeSecurityError),
        ):
            remove_private_file(destination, min_age_seconds=60)
        self.assertTrue(remove_private_file(destination, min_age_seconds=60))
        with self.assertRaises(RuntimeSecurityError):
            cleanup_private_basename_after_ttl(
                "../open-escape.txt",
                0.01,
                expected_device=1,
                expected_inode=1,
                expected_mtime_ns=1,
            )

    def test_cleanup_helper_does_not_delete_same_name_replacement(self):
        destination = text_path("replacement-race")
        atomic_write_bytes(destination, b"old")
        schedule_private_cleanup(
            destination,
            ttl_seconds=0.15,
            cli_path=ROOT / "bin/dank-lookup",
        )
        atomic_write_bytes(destination, b"new")
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            time.sleep(0.02)
            self.assertTrue(destination.exists(), "cleanup helper deleted the replacement")
        self.assertEqual(destination.read_bytes(), b"new")


if __name__ == "__main__":
    unittest.main()
