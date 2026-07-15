#!/usr/bin/env python3
"""Bounded stdin protocol for sdcv 0.5.5.

This module is shared by the mandatory feasibility probe and the production
adapter. It relies only on fixed C-locale prompts proven by the isolated gate;
unknown or incomplete frames are rejected rather than guessed.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import selectors
import signal
import subprocess
import threading
import time
from typing import Any


DEFAULT_STDOUT_CAP_BYTES = 64 * 1024 * 1024
STDERR_CAP_BYTES = 1024 * 1024
MAIN_PROMPT = b"Enter word or phrase: "
CHOICE_PROMPT = b"Your choice[-1 to abort]: "


class SdcvProtocolError(RuntimeError):
    pass


def protocol_argv(
    executable: str,
    *,
    data_dir: str | None,
    dictionaries: list[str],
    exact: bool,
) -> list[str]:
    argv = [executable, "--json", "--utf8-input", "--utf8-output"]
    if data_dir:
        argv.extend(["--data-dir", os.path.expanduser(data_dir), "--only-data-dir"])
    if exact:
        argv.append("--exact-search")
    for dictionary in dictionaries:
        argv.extend(["--use-dict", dictionary])
    return argv


def parse_protocol_stdout(
    stdout: bytes,
    *,
    query: str,
    choice: int | None = None,
) -> dict[str, Any]:
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SdcvProtocolError("stdout_not_utf8") from exc
    prompt = MAIN_PROMPT.decode("ascii")
    terminal_prompt = prompt + "\n"
    expected_prefix = f"{prompt}{query}\n"
    if not text.startswith(expected_prefix):
        raise SdcvProtocolError("missing_initial_prompt_frame")
    if not text.endswith(terminal_prompt):
        raise SdcvProtocolError("missing_terminal_prompt_frame")
    framed = text[len(expected_prefix) : -len(terminal_prompt)]
    decisions = ["main_prompt", "query_echo"]
    options: list[dict[str, Any]] = []
    if choice is None:
        payload = framed.strip()
    else:
        marker = f"{CHOICE_PROMPT.decode('ascii')}{choice}\n"
        if framed.count(marker) != 1:
            raise SdcvProtocolError("ambiguous_choice_frame")
        option_text, selected_text = framed.split(marker, 1)
        if not option_text.startswith("["):
            raise SdcvProtocolError("invalid_fuzzy_option_frame")
        for line in option_text[1:].splitlines():
            match = re.fullmatch(r"(\d+)\)(.*?)-->(.*)", line)
            if match is None:
                raise SdcvProtocolError("invalid_fuzzy_option_line")
            options.append(
                {
                    "index": int(match.group(1)),
                    "dictionary": match.group(2),
                    "word": match.group(3),
                }
            )
        if not options or [item["index"] for item in options] != list(range(len(options))):
            raise SdcvProtocolError("invalid_fuzzy_option_indexes")
        payload = "[" + selected_text.strip()
        decisions.extend(["fuzzy_options", "choice_prompt", f"choice:{choice}"])
    try:
        entries = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SdcvProtocolError("invalid_result_json") from exc
    if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
        raise SdcvProtocolError("invalid_result_shape")
    decisions.extend(["json_result", "terminal_main_prompt", "stdin_eof"])
    return {"entries": entries, "options": options, "decisions": decisions}


def parse_empty_eof(stdout: bytes) -> dict[str, Any]:
    if stdout != MAIN_PROMPT + b"\n":
        raise SdcvProtocolError("invalid_top_level_eof")
    return {"entries": [], "options": [], "decisions": ["main_prompt", "stdin_eof"]}


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _process_group_reaped(process_group: int, timeout_seconds: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.01)
    return False


def run_bounded_protocol(
    *,
    name: str,
    argv: list[str],
    environment: dict[str, str],
    query: str | None,
    choice: int | None = None,
    close_after_query: bool = False,
    close_on_initial_prompt: bool = False,
    timeout_seconds: float = 5.0,
    cancel_after_seconds: float | None = None,
    cancellation_event: threading.Event | None = None,
    stdout_cap_bytes: int = DEFAULT_STDOUT_CAP_BYTES,
    stderr_cap_bytes: int = STDERR_CAP_BYTES,
    inspect_query_cmdline: bool = False,
    evidence_dir: pathlib.Path | None = None,
) -> tuple[dict[str, Any], bytes]:
    stdout_cap_bytes = max(1, int(stdout_cap_bytes))
    stderr_cap_bytes = max(1, int(stderr_cap_bytes))
    stdout_name = f"{name}.stdout.bin"
    stderr_name = f"{name}.stderr.bin"
    cmdline_name = f"{name}.cmdline.bin"
    started_ns = time.monotonic_ns()
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        start_new_session=True,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    for stream, label in ((process.stdout, "stdout"), (process.stderr, "stderr")):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, label)
    deadline = time.monotonic() + max(0.01, timeout_seconds)
    cancel_at = (
        time.monotonic() + cancel_after_seconds
        if cancel_after_seconds is not None
        else None
    )
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    stdout_observed = 0
    stderr_observed = 0
    main_prompts = 0
    choice_prompts = 0
    main_search_from = 0
    choice_search_from = 0
    query_sent = False
    choice_sent = False
    stdin_closed = False
    killed = False
    termination_reason: str | None = None
    cmdline = b""
    decisions: list[str] = []

    def close_stdin() -> None:
        nonlocal stdin_closed
        if stdin_closed:
            return
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
        stdin_closed = True
        decisions.append("stdin_closed")

    def send(payload: bytes, decision: str) -> None:
        if stdin_closed:
            raise SdcvProtocolError("write_after_stdin_close")
        try:
            process.stdin.write(payload)
            process.stdin.flush()
        except BrokenPipeError as exc:
            raise SdcvProtocolError("early_stdin_close") from exc
        decisions.append(decision)

    def kill(reason: str) -> None:
        nonlocal killed, termination_reason
        if killed:
            return
        killed = True
        termination_reason = reason
        decisions.append(reason)
        close_stdin()
        _kill_process_group(process)
        for selector_key in list(selector.get_map().values()):
            try:
                selector.unregister(selector_key.fileobj)
            except (KeyError, ValueError):
                pass

    try:
        while selector.get_map() or process.poll() is None:
            now = time.monotonic()
            if cancellation_event is not None and cancellation_event.is_set():
                kill("cancelled")
            if cancel_at is not None and now >= cancel_at:
                kill("cancelled")
            if now >= deadline:
                kill("timeout")
            events = selector.select(0.01)
            for key, _mask in events:
                if killed:
                    break
                observed = stdout_observed if key.data == "stdout" else stderr_observed
                cap_bytes = stdout_cap_bytes if key.data == "stdout" else stderr_cap_bytes
                read_size = min(64 * 1024, max(1, cap_bytes - observed + 1))
                try:
                    chunk = os.read(key.fileobj.fileno(), read_size)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stdout":
                    stdout_observed += len(chunk)
                    remaining = max(0, stdout_cap_bytes - len(stdout_buffer))
                    stdout_buffer.extend(chunk[:remaining])
                    while True:
                        position = stdout_buffer.find(MAIN_PROMPT, main_search_from)
                        if position < 0:
                            main_search_from = max(0, len(stdout_buffer) - len(MAIN_PROMPT) + 1)
                            break
                        main_prompts += 1
                        main_search_from = position + len(MAIN_PROMPT)
                    while True:
                        position = stdout_buffer.find(CHOICE_PROMPT, choice_search_from)
                        if position < 0:
                            choice_search_from = max(0, len(stdout_buffer) - len(CHOICE_PROMPT) + 1)
                            break
                        choice_prompts += 1
                        choice_search_from = position + len(CHOICE_PROMPT)
                    if stdout_observed > stdout_cap_bytes:
                        kill("stdout_cap")
                else:
                    stderr_observed += len(chunk)
                    remaining = max(0, stderr_cap_bytes - len(stderr_buffer))
                    stderr_buffer.extend(chunk[:remaining])
                    if stderr_observed > stderr_cap_bytes:
                        kill("stderr_cap")
            if main_prompts >= 1 and not query_sent:
                if query is None:
                    if close_on_initial_prompt:
                        close_stdin()
                else:
                    send(query.encode("utf-8") + b"\n", "query_sent")
                    query_sent = True
                    if inspect_query_cmdline:
                        try:
                            cmdline = pathlib.Path(f"/proc/{process.pid}/cmdline").read_bytes()
                        except OSError:
                            cmdline = b""
                    if close_after_query:
                        close_stdin()
            if choice is not None and choice_prompts >= 1 and not choice_sent:
                send(f"{choice}\n".encode("ascii"), f"choice_sent:{choice}")
                choice_sent = True
            if query_sent and main_prompts >= 2 and not stdin_closed:
                close_stdin()
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            kill("wait_timeout")
            process.wait(timeout=1.0)
    finally:
        selector.close()
        if process.poll() is None:
            kill("runner_error")
            process.wait(timeout=1.0)
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass
    group_reaped = _process_group_reaped(process.pid)
    duration_ms = round((time.monotonic_ns() - started_ns) / 1_000_000, 3)
    if evidence_dir is not None:
        evidence_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        (evidence_dir / stdout_name).write_bytes(stdout_buffer)
        (evidence_dir / stderr_name).write_bytes(stderr_buffer)
        if inspect_query_cmdline:
            (evidence_dir / cmdline_name).write_bytes(cmdline)
    result = {
        "name": name,
        "argv_metadata": argv[1:],
        "query_bytes": len(query.encode("utf-8")) if query is not None else 0,
        "choice": choice,
        "pid": process.pid,
        "returncode": process.returncode,
        "duration_ms": duration_ms,
        "termination_reason": termination_reason,
        "stdout_bytes": stdout_observed,
        "stdout_stored_bytes": len(stdout_buffer),
        "stderr_bytes": stderr_observed,
        "stderr_stored_bytes": len(stderr_buffer),
        "stdout_cap_bytes": stdout_cap_bytes,
        "stderr_cap_bytes": stderr_cap_bytes,
        "stdout_file": stdout_name,
        "stderr_file": stderr_name,
        "cmdline_file": cmdline_name if inspect_query_cmdline else None,
        "query_in_cmdline": (
            query.encode("utf-8") in cmdline
            if inspect_query_cmdline and query is not None and cmdline
            else False
        ),
        "main_prompt_count": main_prompts,
        "choice_prompt_count": choice_prompts,
        "process_group_reaped": group_reaped,
        "runner_decisions": decisions,
    }
    return result, bytes(stdout_buffer)
