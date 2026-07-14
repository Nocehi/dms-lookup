#!/usr/bin/env python3
"""Verify that a runtime-generated query never enters process metadata or artifacts."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Iterable


ROOT = pathlib.Path(__file__).resolve().parent.parent
CLI = ROOT / "bin/dank-lookup"
SELECTION = ROOT / "bin/dank-lookup-selection"


class PrivacyFailure(RuntimeError):
    pass


def write_executable(path: pathlib.Path, body: str) -> pathlib.Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o700)
    return path


def read_proc(pid: int, name: str) -> bytes:
    try:
        return (pathlib.Path("/proc") / str(pid) / name).read_bytes()
    except OSError:
        return b""


def executable_name(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return "unavailable"


def direct_children(pid: int) -> set[int]:
    children: set[int] = set()
    task_root = pathlib.Path(f"/proc/{pid}/task")
    try:
        tasks = list(task_root.iterdir())
    except OSError:
        return children
    for task in tasks:
        try:
            value = (task / "children").read_text(encoding="ascii").strip()
        except OSError:
            continue
        children.update(int(item) for item in value.split() if item.isdigit())
    return children


def wait_for_socket(path: pathlib.Path, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise PrivacyFailure(
                f"stage=service pid={process.pid} executable={executable_name(process.pid)} "
                "location=early_exit"
            )
        if path.is_socket():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(0.05)
                probe.connect(str(path))
                return
            except OSError:
                pass
            finally:
                probe.close()
        time.sleep(0.01)
    raise PrivacyFailure(
        f"stage=service pid={process.pid} executable={executable_name(process.pid)} "
        "location=startup_timeout"
    )


def run_verifier(scan_paths: Iterable[pathlib.Path]) -> None:
    with tempfile.TemporaryDirectory(prefix="dank-lookup-privacy-") as temporary:
        temp = pathlib.Path(temporary)
        runtime = temp / "runtime"
        home = temp / "home"
        commands = temp / "commands"
        for directory in (runtime, home, commands):
            directory.mkdir(mode=0o700)

        fake_wl_paste = write_executable(
            commands / "wl-paste",
            """#!/bin/sh
set -eu
payload=$(cat)
sleep 0.12
printf '%s' "$payload"
""",
        )
        fake_sdcv = write_executable(
            commands / "fixture-sdcv",
            """#!/bin/sh
set -eu
for argument do
    if [ "$argument" = "--list-dicts" ]; then
        printf "%s\\n" "Dictionary's name   Word count" "FakeDict    1"
        exit 0
    fi
done
printf '%s' 'Enter word or phrase: '
IFS= read -r query
sleep 0.15
printf '%s\\n' "$query"
printf '%s\\n' '[]'
printf '%s\\n' 'Enter word or phrase: '
""",
        )
        fake_tokenizer = write_executable(
            commands / "fixture-tokenizer",
            """#!/bin/sh
set -eu
IFS= read -r query
sleep 0.30
printf '%s\\n' '{"tokens":[]}'
""",
        )
        config = temp / "config.json"
        config.write_text(
            json.dumps(
                {
                    "version": 2,
                    "source_timeout_ms": 1000,
                    "lookup_timeout_ms": 4000,
                    "modes": {
                        "han": {
                            "label": "Han",
                            "default_for": ["han"],
                            "sources": [
                                {
                                    "name": "FakeDict",
                                    "when": "any",
                                    "allow_related": False,
                                }
                            ],
                        }
                    },
                    "fallback": {"mock": False, "unfiltered": False},
                }
            ),
            encoding="utf-8",
        )

        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_DATA_HOME": str(home / ".local/share"),
                "XDG_RUNTIME_DIR": str(runtime),
                "PATH": str(commands) + os.pathsep + environment["PATH"],
                "DANK_LOOKUP_LIBDIR": str(ROOT / "lib"),
                "DANK_LOOKUP_CONFIG": str(config),
                "DANK_LOOKUP_SDCV": str(fake_sdcv),
                "DANK_LOOKUP_TOKENIZER": str(fake_tokenizer),
                "DANK_LOOKUP_BACKEND_IDLE_TIMEOUT": "0.2",
                "SDCV_HISTFILE": "/dev/null",
                "SDCV_HISTSIZE": "0",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )

        # The sentinel is born only after every command, path, fixture, and
        # environment value is fixed. Only its encoded query form is injected.
        raw_sentinel = secrets.token_bytes(24)
        query = "漢-" + raw_sentinel.hex()
        protected_values = (raw_sentinel, query.encode("utf-8"))
        failures: list[str] = []
        snapshots: set[tuple[str, int]] = set()

        def contains_secret(data: bytes) -> bool:
            return any(value and value in data for value in protected_values)

        def fail(stage: str, pid: int, location: str) -> None:
            failures.append(
                f"stage={stage} pid={pid} executable={executable_name(pid)} "
                f"location={location}"
            )

        def snapshot(stage: str, pid: int, expected_path: pathlib.Path) -> None:
            key = (stage, pid)
            if key in snapshots:
                return
            snapshots.add(key)
            expected = str(expected_path).encode("utf-8")
            identity_deadline = time.monotonic() + 0.25
            cmdline = read_proc(pid, "cmdline")
            # A shebang child initially has the same directly captured PID but
            # may briefly expose /usr/bin/env before exec reaches the script.
            # Wait only on that PID; never search by process name or sentinel.
            while expected not in cmdline and time.monotonic() < identity_deadline:
                if not pathlib.Path(f"/proc/{pid}").exists():
                    break
                time.sleep(0.001)
                cmdline = read_proc(pid, "cmdline")
            if expected not in cmdline:
                fail(stage, pid, "executable_identity")
            for location, data in (
                ("cmdline", cmdline),
                ("environment", read_proc(pid, "environ")),
                ("process_title", read_proc(pid, "comm")),
            ):
                if contains_secret(data):
                    fail(stage, pid, location)

        if contains_secret(read_proc(os.getpid(), "cmdline")):
            fail("test_runner", os.getpid(), "cmdline")
        if contains_secret(read_proc(os.getpid(), "environ")):
            fail("test_runner", os.getpid(), "environment")

        selection_log = (temp / "selection.stderr.log").open("wb")
        selection = subprocess.Popen(
            [str(SELECTION), "--capture"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=selection_log,
            env=environment,
        )
        try:
            assert selection.stdin is not None
            selection.stdin.write(query.encode("utf-8"))
            selection.stdin.close()
            selection.stdin = None
            snapshot("selection_helper", selection.pid, SELECTION)
            if selection.wait(timeout=2.0) != 0:
                fail("selection_helper", selection.pid, "exit_status")
        finally:
            if selection.poll() is None:
                selection.kill()
                selection.wait(timeout=1.0)
            selection_log.close()

        service_log = (temp / "service.log").open("wb")
        service = subprocess.Popen(
            [
                sys.executable,
                str(CLI),
                "backend-serve",
                "--idle-timeout",
                "0.2",
            ],
            stdin=subprocess.DEVNULL,
            stdout=service_log,
            stderr=subprocess.STDOUT,
            env=environment,
        )
        client_log = (temp / "client.stderr.log").open("wb")
        client: subprocess.Popen[bytes] | None = None
        try:
            socket_path = runtime / "dank-lookup/backend.sock"
            wait_for_socket(socket_path, service)
            envelope = {
                "action": "initial_selection",
                "request_id": str(uuid.uuid4()),
                "sequence": 1,
                "source": "primary",
                "mode": "han",
                "query": query,
            }
            client = subprocess.Popen(
                [sys.executable, str(CLI), "backend-request", "--timeout", "5"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=client_log,
                env=environment,
            )
            assert client.stdin is not None
            client.stdin.write(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))
            client.stdin.close()
            client.stdin = None
            snapshot("ipc_client", client.pid, CLI)
            snapshot("service_receiver", service.pid, CLI)

            seen_sdcv = False
            seen_tokenizer = False
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline and client.poll() is None:
                for child_pid in direct_children(service.pid):
                    cmdline = read_proc(child_pid, "cmdline")
                    if str(fake_sdcv).encode("utf-8") in cmdline and b"--json" in cmdline:
                        snapshot("sdcv", child_pid, fake_sdcv)
                        seen_sdcv = True
                    if str(fake_tokenizer).encode("utf-8") in cmdline:
                        snapshot("tokenizer", child_pid, fake_tokenizer)
                        seen_tokenizer = True
                time.sleep(0.005)
            if client.wait(timeout=1.0) != 0:
                fail("ipc_client", client.pid, "exit_status")
            if not seen_sdcv:
                fail("sdcv", service.pid, "stage_not_observed")
            if not seen_tokenizer:
                fail("tokenizer", service.pid, "stage_not_observed")
            try:
                service.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                fail("service_receiver", service.pid, "idle_exit_timeout")
        finally:
            if client is not None and client.poll() is None:
                client.kill()
                client.wait(timeout=1.0)
            if service.poll() is None:
                service.kill()
                service.wait(timeout=1.0)
            client_log.close()
            service_log.close()

        for path in temp.rglob("*"):
            if contains_secret(path.name.encode("utf-8", errors="surrogateescape")):
                fail("artifacts", os.getpid(), "filename")
            if not path.is_file():
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if contains_secret(data):
                fail("artifacts", os.getpid(), "file_content")
            if "history" in path.name.casefold():
                fail("sdcv", os.getpid(), "history_file")

        for scan_path in scan_paths:
            candidates = scan_path.rglob("*") if scan_path.is_dir() else (scan_path,)
            for path in candidates:
                if contains_secret(path.name.encode("utf-8", errors="surrogateescape")):
                    fail("external_scan", os.getpid(), "filename")
                if path.is_file():
                    try:
                        if contains_secret(path.read_bytes()):
                            fail("external_scan", os.getpid(), "file_content")
                    except OSError:
                        pass

        required_stages = {"selection_helper", "ipc_client", "service_receiver", "sdcv", "tokenizer"}
        observed_stages = {stage for stage, _pid in snapshots}
        for missing in sorted(required_stages - observed_stages):
            fail(missing, os.getpid(), "stage_not_observed")
        if failures:
            raise PrivacyFailure("; ".join(failures))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan-path", action="append", type=pathlib.Path, default=[])
    args = parser.parse_args(argv)
    try:
        run_verifier(args.scan_path)
    except Exception as exc:
        print(f"PRIVACY_TRANSPORT: FAIL: {exc}", file=sys.stderr)
        return 1
    print("PRIVACY_TRANSPORT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
