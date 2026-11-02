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
    if not text.startswith(prompt):
        raise SdcvProtocolError("missing_initial_prompt_frame")
    if not text.endswith(terminal_prompt):
        raise SdcvProtocolError("missing_terminal_prompt_frame")
    return parse_frame(text[len(prompt) : -len(terminal_prompt)], query=query, choice=choice)


def parse_frame(
    frame: str | bytes,
    *,
    query: str,
    choice: int | None = None,
) -> dict[str, Any]:
    """Parse one query frame: the stdout between two main prompts.

    A frame starts with sdcv's echo of the query line and ends just before
    the next ``Enter word or phrase: `` prompt. ``parse_protocol_stdout``
    is the one-shot wrapper; ``SdcvSession.ask`` yields frames directly.
    """
    if isinstance(frame, bytes):
        try:
            frame = frame.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SdcvProtocolError("stdout_not_utf8") from exc
    expected_prefix = f"{query}\n"
    if not frame.startswith(expected_prefix):
        raise SdcvProtocolError("missing_initial_prompt_frame")
    framed = frame[len(expected_prefix) :]
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
    choice_aborted = False
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
            elif choice is None and choice_prompts >= 1 and not choice_sent:
                # An exact search with several homograph entries makes sdcv
                # prompt for a choice. Nobody would answer, so the source
                # used to block until its timeout and exhaust the lookup
                # budget. Abort the prompt; the caller re-queries each index.
                send(b"-1\n", "choice_aborted")
                choice_sent = True
                choice_aborted = True
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
        "choice_aborted": choice_aborted,
        "process_group_reaped": group_reaped,
        "runner_decisions": decisions,
    }
    return result, bytes(stdout_buffer)


class SdcvSession:
    """One sdcv process answering several stdin queries in sequence.

    sdcv's interactive loop (``while (io->read("Enter word or phrase: ", …))``
    in ``sdcv.cpp``) reads lines until stdin EOF, so a single process can
    serve an exact query, its homograph choices, a ``/fuzzy`` listing and
    every candidate re-query without reloading the dictionary index. Each
    ``ask`` returns one frame (the bytes between two main prompts), parsed by
    :func:`parse_frame` exactly as :func:`parse_protocol_stdout` parses a
    one-shot run.

    The process is started lazily by the first ``ask`` and restarted by the
    next ``ask`` after a kill (timeout, cap, cancellation) or an unexpected
    exit, so a failed frame costs no more than the one-shot protocol did.
    ``close`` ends the process by stdin EOF and verifies the process group
    is gone.
    """

    def __init__(
        self,
        *,
        name: str,
        argv: list[str],
        environment: dict[str, str],
        cancellation_event: threading.Event | None = None,
        stderr_cap_bytes: int = STDERR_CAP_BYTES,
        evidence_dir: pathlib.Path | None = None,
    ) -> None:
        self.name = name
        self.argv = list(argv)
        self.environment = dict(environment)
        self.cancellation_event = cancellation_event
        self.stderr_cap_bytes = max(1, int(stderr_cap_bytes))
        self.evidence_dir = evidence_dir
        self.process: subprocess.Popen[bytes] | None = None
        self.starts = 0
        self.frames = 0
        self.pids: list[int] = []
        self._selector: selectors.BaseSelector | None = None
        self._pending = bytearray()
        self._stderr_buffer = bytearray()
        self._stderr_observed = 0
        self._stdout_evidence = bytearray()
        self._killed = False
        self._termination_reason: str | None = None
        self._stdin_closed = False
        self._prompt_ready = False
        self._decisions: list[str] = []

    @property
    def alive(self) -> bool:
        return self.process is not None and not self._killed and self.process.poll() is None

    # -- process lifecycle -------------------------------------------------

    def _spawn(self) -> None:
        self.process = subprocess.Popen(
            self.argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.environment,
            start_new_session=True,
        )
        assert self.process.stdout is not None and self.process.stderr is not None
        self.starts += 1
        self.pids.append(self.process.pid)
        self._selector = selectors.DefaultSelector()
        for stream, label in ((self.process.stdout, "stdout"), (self.process.stderr, "stderr")):
            os.set_blocking(stream.fileno(), False)
            self._selector.register(stream, selectors.EVENT_READ, label)
        self._pending = bytearray()
        self._killed = False
        self._termination_reason = None
        self._stdin_closed = False
        self._prompt_ready = False
        self._decisions.append("session_started")

    def _close_stdin(self) -> None:
        if self.process is None or self._stdin_closed:
            return
        try:
            self.process.stdin.close()  # type: ignore[union-attr]
        except (BrokenPipeError, OSError):
            pass
        self._stdin_closed = True
        self._decisions.append("stdin_closed")

    def _kill(self, reason: str) -> None:
        if self.process is None or self._killed:
            return
        self._killed = True
        self._termination_reason = reason
        self._decisions.append(reason)
        self._close_stdin()
        _kill_process_group(self.process)
        self._release_streams()

    def _release_streams(self) -> None:
        if self._selector is not None:
            for key in list(self._selector.get_map().values()):
                try:
                    self._selector.unregister(key.fileobj)
                except (KeyError, ValueError):
                    pass
            self._selector.close()
            self._selector = None
        if self.process is not None:
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass

    def _finish_process(self, wait_seconds: float = 1.0) -> tuple[int | None, bool]:
        """Wait for the process, kill it if it lingers, and check the group."""
        if self.process is None:
            return None, True
        try:
            self.process.wait(timeout=wait_seconds)
        except subprocess.TimeoutExpired:
            self._kill("wait_timeout")
            try:
                self.process.wait(timeout=wait_seconds)
            except subprocess.TimeoutExpired:
                pass
        self._release_streams()
        reaped = _process_group_reaped(self.process.pid)
        returncode = self.process.returncode
        process = self.process
        self.process = None
        self._killed = True
        del process
        return returncode, reaped

    def _send(self, payload: bytes, decision: str) -> bool:
        assert self.process is not None and self.process.stdin is not None
        if self._stdin_closed:
            return False
        try:
            self.process.stdin.write(payload)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            self._decisions.append("early_stdin_close")
            return False
        self._decisions.append(decision)
        return True

    # -- reading -----------------------------------------------------------

    def _pump(self, deadline: float, frame_cap_bytes: int, frame_observed: int) -> tuple[int, bool]:
        """Read one round of output; return (frame bytes observed, eof)."""
        assert self.process is not None and self._selector is not None
        now = time.monotonic()
        if self.cancellation_event is not None and self.cancellation_event.is_set():
            self._kill("cancelled")
            return frame_observed, True
        if now >= deadline:
            self._kill("timeout")
            return frame_observed, True
        events = self._selector.select(0.01)
        eof = False
        for key, _mask in events:
            if self._killed:
                break
            if key.data == "stdout":
                read_size = min(64 * 1024, max(1, frame_cap_bytes - frame_observed + 1))
            else:
                read_size = min(64 * 1024, max(1, self.stderr_cap_bytes - self._stderr_observed + 1))
            try:
                chunk = os.read(key.fileobj.fileno(), read_size)  # type: ignore[union-attr]
            except BlockingIOError:
                continue
            except OSError:
                chunk = b""
            if not chunk:
                try:
                    self._selector.unregister(key.fileobj)
                except (KeyError, ValueError):
                    pass
                if key.data == "stdout":
                    eof = True
                continue
            if key.data == "stdout":
                frame_observed += len(chunk)
                self._pending.extend(chunk)
                if self.evidence_dir is not None:
                    self._stdout_evidence.extend(chunk)
                if frame_observed > frame_cap_bytes:
                    self._kill("stdout_cap")
                    return frame_observed, True
            else:
                self._stderr_observed += len(chunk)
                remaining = max(0, self.stderr_cap_bytes - len(self._stderr_buffer))
                self._stderr_buffer.extend(chunk[:remaining])
                if self._stderr_observed > self.stderr_cap_bytes:
                    self._kill("stderr_cap")
                    return frame_observed, True
        if not eof and self._selector is not None and not self._selector.get_map():
            eof = True
        return frame_observed, eof

    def _wait_for_prompt(self, deadline: float, frame_cap_bytes: int) -> tuple[int, bool]:
        """Consume output until the initial main prompt is pending."""
        observed = 0
        while True:
            if self._pending.endswith(MAIN_PROMPT):
                self._pending = bytearray()
                self._prompt_ready = True
                self._decisions.append("main_prompt")
                return observed, False
            observed, eof = self._pump(deadline, frame_cap_bytes, observed)
            if eof or self._killed:
                return observed, True

    # -- public API --------------------------------------------------------

    def ask(
        self,
        query: str,
        *,
        choice: int | None = None,
        timeout_seconds: float = 5.0,
        stdout_cap_bytes: int = DEFAULT_STDOUT_CAP_BYTES,
    ) -> tuple[dict[str, Any], bytes]:
        """Send one query line and return (run metadata, frame bytes).

        ``choice`` answers sdcv's ``Your choice[-1 to abort]: `` prompt; when
        it is ``None`` and the prompt appears anyway (exact homographs), the
        prompt is aborted with ``-1`` and ``choice_aborted`` is set so the
        caller can re-query each index.
        """
        frame_cap_bytes = max(1, int(stdout_cap_bytes))
        started_ns = time.monotonic_ns()
        deadline = time.monotonic() + max(0.01, timeout_seconds)
        self._decisions = []
        restarted = False
        if not self.alive or self._stdin_closed:
            if self.process is not None:
                self._finish_process(0.1)
            restarted = self.starts > 0
            try:
                self._spawn()
            except OSError as exc:
                raise SdcvProtocolError("spawn_failed") from exc
        assert self.process is not None
        pid = self.process.pid
        observed = 0
        eof = False
        choice_prompts = 0
        choice_sent = False
        choice_aborted = False
        frame = b""
        if not self._prompt_ready:
            observed, eof = self._wait_for_prompt(deadline, frame_cap_bytes)
        if self._prompt_ready and not eof:
            self._prompt_ready = False
            if self._send(query.encode("utf-8") + b"\n", "query_sent"):
                main_search_from = 0
                choice_search_from = 0
                while True:
                    position = self._pending.find(MAIN_PROMPT, main_search_from)
                    if position >= 0:
                        frame = bytes(self._pending[:position])
                        del self._pending[: position + len(MAIN_PROMPT)]
                        self._prompt_ready = True
                        self._decisions.append("terminal_main_prompt")
                        break
                    main_search_from = max(0, len(self._pending) - len(MAIN_PROMPT) + 1)
                    if not choice_sent:
                        choice_position = self._pending.find(CHOICE_PROMPT, choice_search_from)
                        if choice_position >= 0:
                            choice_prompts += 1
                            choice_search_from = choice_position + len(CHOICE_PROMPT)
                            if choice is not None:
                                choice_sent = self._send(f"{choice}\n".encode("ascii"), f"choice_sent:{choice}")
                            else:
                                choice_sent = self._send(b"-1\n", "choice_aborted")
                                choice_aborted = True
                            if not choice_sent:
                                eof = True
                                break
                        else:
                            choice_search_from = max(0, len(self._pending) - len(CHOICE_PROMPT) + 1)
                    observed, eof = self._pump(deadline, frame_cap_bytes, observed)
                    if eof or self._killed:
                        break
            else:
                eof = True
        returncode: int | None = None
        reaped: bool | None = None
        if eof or self._killed:
            # The process is gone or has been killed: reap it now so the
            # caller sees the same termination reasons as the one-shot
            # protocol, and the next ask starts a fresh process.
            frame = bytes(self._pending)
            self._pending = bytearray()
            returncode, reaped = self._finish_process()
            self._prompt_ready = False
            if self._termination_reason is None and self._decisions and self._decisions[-1] == "early_stdin_close":
                self._termination_reason = "early_stdin_close"
        self.frames += 1
        duration_ms = round((time.monotonic_ns() - started_ns) / 1_000_000, 3)
        run = {
            "name": self.name,
            "argv_metadata": self.argv[1:],
            "query_bytes": len(query.encode("utf-8")),
            "choice": choice,
            "pid": pid,
            "session_start": self.starts,
            "session_restarted": restarted,
            "session_frame": self.frames,
            "returncode": returncode,
            "duration_ms": duration_ms,
            "termination_reason": self._termination_reason,
            "stdout_bytes": observed,
            "stdout_stored_bytes": len(frame),
            "stderr_bytes": self._stderr_observed,
            "stderr_stored_bytes": len(self._stderr_buffer),
            "stdout_cap_bytes": frame_cap_bytes,
            "stderr_cap_bytes": self.stderr_cap_bytes,
            "main_prompt_count": 1 if self._prompt_ready else 0,
            "choice_prompt_count": choice_prompts,
            "choice_aborted": choice_aborted,
            "process_group_reaped": reaped,
            "session_alive": self.alive,
            "runner_decisions": list(self._decisions),
        }
        return run, frame

    def close(self, timeout_seconds: float = 1.0) -> dict[str, Any]:
        """End the process by stdin EOF and report how it went."""
        started_ns = time.monotonic_ns()
        self._decisions = []
        returncode: int | None = None
        reaped = True
        termination = self._termination_reason
        if self.process is not None:
            if self.alive:
                self._close_stdin()
                deadline = time.monotonic() + max(0.01, timeout_seconds)
                observed = 0
                while True:
                    observed, eof = self._pump(deadline, DEFAULT_STDOUT_CAP_BYTES, observed)
                    if eof or self._killed:
                        break
                termination = self._termination_reason
            returncode, reaped = self._finish_process(timeout_seconds)
        if self.evidence_dir is not None:
            self.evidence_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            (self.evidence_dir / f"{self.name}.stdout.bin").write_bytes(self._stdout_evidence)
            (self.evidence_dir / f"{self.name}.stderr.bin").write_bytes(self._stderr_buffer)
        return {
            "name": self.name,
            "returncode": returncode,
            "termination_reason": termination,
            "duration_ms": round((time.monotonic_ns() - started_ns) / 1_000_000, 3),
            "process_group_reaped": reaped,
            "session_starts": self.starts,
            "session_frames": self.frames,
            "pids": list(self.pids),
            "runner_decisions": list(self._decisions),
        }
