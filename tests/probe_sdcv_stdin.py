#!/usr/bin/env python3
"""Probe the installed sdcv stdin protocol using isolated benign dictionaries.

The discovery mode is intentionally non-production: it records the exact raw
stdout/stderr for candidate stdin protocols before any adapter is selected.
Queries are written only to the child stdin and never appended to its argv.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import secrets
import selectors
import signal
import struct
import subprocess
import sys
import tempfile
import time
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURE_ROOT = ROOT / "tests/fixtures-sdcv-stdin"
sys.path.insert(0, str(ROOT / "lib"))

from dank_lookup_sdcv_stdin import (  # noqa: E402
    DEFAULT_STDOUT_CAP_BYTES as PRODUCTION_STDOUT_CAP_BYTES,
    STDERR_CAP_BYTES as PRODUCTION_STDERR_CAP_BYTES,
    SdcvProtocolError as ProductionProtocolError,
    parse_empty_eof as production_parse_empty_eof,
    parse_protocol_stdout as production_parse_protocol_stdout,
    protocol_argv as production_protocol_argv,
    run_bounded_protocol as production_run_bounded_protocol,
)
DEFAULT_CAP_BYTES = PRODUCTION_STDOUT_CAP_BYTES
STDERR_CAP_BYTES = PRODUCTION_STDERR_CAP_BYTES
SDCV_REQUIRED_VERSION = "0.5.5"
SDCV_VERSION_PATTERN = re.compile(
    r"\bversion\s+(\d+(?:\.\d+){2})(?![A-Za-z0-9_.+-])", re.IGNORECASE
)
MAIN_PROMPT = b"Enter word or phrase: "
CHOICE_PROMPT = b"Your choice[-1 to abort]: "
GIANT_UNDER_BYTES = 32 * 1024 * 1024
GIANT_OVER_BYTES = 65 * 1024 * 1024
GIANT_UNDER_BOOK = "DANK_STDIN_GIANT32"
GIANT_OVER_BOOK = "DANK_STDIN_GIANT_OVER"


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def serialize_report(report: dict[str, Any]) -> str:
    return (
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )


def parse_sdcv_version(version_raw: str) -> str:
    version_match = SDCV_VERSION_PATTERN.search(version_raw)
    return version_match.group(1) if version_match is not None else ""


def build_gate_report(
    *,
    version_returncode: int,
    version_raw: str,
    history_paths: list[str],
    cases: list[dict[str, Any]],
) -> dict[str, Any]:
    parsed_version = parse_sdcv_version(version_raw)
    gate_errors = []
    if version_returncode != 0 or parsed_version != SDCV_REQUIRED_VERSION:
        gate_errors.append("installed sdcv is not the required 0.5.5 binary")
    if history_paths:
        gate_errors.append("sdcv history files were created")
    failed_cases = [case["name"] for case in cases if case["status"] != "pass"]
    if failed_cases:
        gate_errors.append("failed cases: " + ", ".join(failed_cases))
    return {
        "schema_version": 2,
        "kind": "sdcv_0_5_5_stdin_feasibility_gate",
        "verdict": "pass" if not gate_errors else "fail",
        "sdcv_version": parsed_version,
        "sdcv_version_raw": version_raw,
        "sdcv_version_returncode": version_returncode,
        "stdout_cap_bytes": DEFAULT_CAP_BYTES,
        "stderr_cap_bytes": STDERR_CAP_BYTES,
        "history_environment": {"SDCV_HISTFILE": "/dev/null", "SDCV_HISTSIZE": "0"},
        "history_paths": history_paths,
        "gate_errors": gate_errors,
        "cases": cases,
    }


def build_dictionary(directory: pathlib.Path, dictionary: dict[str, Any]) -> None:
    bookname = str(dictionary["bookname"])
    stem = directory / bookname.lower()
    entries = sorted(
        (
            str(item["word"]).encode("utf-8"),
            str(item["definition"]).encode("utf-8"),
        )
        for item in dictionary["entries"]
    )
    dictionary_bytes = bytearray()
    index_bytes = bytearray()
    for word, definition in entries:
        offset = len(dictionary_bytes)
        dictionary_bytes.extend(definition)
        index_bytes.extend(word)
        index_bytes.append(0)
        index_bytes.extend(struct.pack(">II", offset, len(definition)))
    stem.with_suffix(".dict").write_bytes(dictionary_bytes)
    stem.with_suffix(".idx").write_bytes(index_bytes)
    stem.with_suffix(".ifo").write_text(
        "\n".join(
            [
                "StarDict's dict ifo file",
                "version=2.4.2",
                f"wordcount={len(entries)}",
                f"idxfilesize={len(index_bytes)}",
                f"bookname={bookname}",
                f"sametypesequence={dictionary.get('sametypesequence', 'm')}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def build_generated_dictionary(
    directory: pathlib.Path,
    *,
    bookname: str,
    word: str,
    definition_bytes: int,
    tail: bytes,
) -> None:
    if definition_bytes < len(tail):
        raise ValueError("definition size is smaller than its tail")
    stem = directory / bookname.lower()
    remaining = definition_bytes - len(tail)
    with stem.with_suffix(".dict").open("wb") as handle:
        chunk = b"x" * (1024 * 1024)
        while remaining:
            amount = min(remaining, len(chunk))
            handle.write(chunk[:amount])
            remaining -= amount
        handle.write(tail)
    word_bytes = word.encode("utf-8")
    index_bytes = word_bytes + b"\0" + struct.pack(">II", 0, definition_bytes)
    stem.with_suffix(".idx").write_bytes(index_bytes)
    stem.with_suffix(".ifo").write_text(
        "\n".join(
            [
                "StarDict's dict ifo file",
                "version=2.4.2",
                "wordcount=1",
                f"idxfilesize={len(index_bytes)}",
                f"bookname={bookname}",
                "sametypesequence=m",
                "",
            ]
        ),
        encoding="utf-8",
    )


def build_fixture_tree(root: pathlib.Path, *, include_giant: bool = False) -> pathlib.Path:
    data_root = root / "stardict"
    dictionary_dir = data_root / "dic"
    dictionary_dir.mkdir(parents=True, mode=0o700)
    spec = load_json(FIXTURE_ROOT / "spec.json")
    for dictionary in spec["dictionaries"]:
        build_dictionary(dictionary_dir, dictionary)
    if include_giant:
        build_generated_dictionary(
            dictionary_dir,
            bookname=GIANT_UNDER_BOOK,
            word="giant-under-cap",
            definition_bytes=GIANT_UNDER_BYTES,
            tail=b"GIANT-32-COMPLETE-TAIL",
        )
        build_generated_dictionary(
            dictionary_dir,
            bookname=GIANT_OVER_BOOK,
            word="giant-over-cap",
            definition_bytes=GIANT_OVER_BYTES,
            tail=b"GIANT-OVER-COMPLETE-TAIL",
        )
    return data_root


def sdcv_argv(executable: str, data_root: pathlib.Path, *, non_interactive: bool) -> list[str]:
    argv = [
        executable,
        "--json",
        "--utf8-input",
        "--utf8-output",
        "--data-dir",
        str(data_root),
        "--only-data-dir",
        "--use-dict",
        "DANK_STDIN_A",
    ]
    if non_interactive:
        argv.insert(1, "--non-interactive")
    return argv


def protocol_argv(
    executable: str,
    data_root: pathlib.Path,
    *,
    dictionaries: list[str],
    exact: bool,
) -> list[str]:
    argv = [
        executable,
        "--json",
        "--utf8-input",
        "--utf8-output",
        "--data-dir",
        str(data_root),
        "--only-data-dir",
    ]
    if exact:
        argv.append("--exact-search")
    for dictionary in dictionaries:
        argv.extend(["--use-dict", dictionary])
    return argv


class ProtocolParseError(ValueError):
    pass


def parse_protocol_stdout(
    stdout: bytes,
    *,
    query: str,
    choice: int | None = None,
) -> dict[str, Any]:
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolParseError("stdout is not UTF-8") from exc
    prompt = MAIN_PROMPT.decode("ascii")
    terminal_prompt = prompt + "\n"
    expected_prefix = f"{prompt}{query}\n"
    if not text.startswith(expected_prefix):
        raise ProtocolParseError("missing deterministic initial prompt/query frame")
    if not text.endswith(terminal_prompt):
        raise ProtocolParseError("missing terminal main prompt before EOF")
    framed = text[len(expected_prefix) : -len(terminal_prompt)]
    decisions = ["main_prompt", "query_echo"]
    options: list[dict[str, Any]] = []
    if choice is None:
        payload = framed.strip()
    else:
        marker = f"{CHOICE_PROMPT.decode('ascii')}{choice}\n"
        if framed.count(marker) != 1:
            raise ProtocolParseError("choice prompt/echo frame is absent or ambiguous")
        option_text, selected_text = framed.split(marker, 1)
        if not option_text.startswith("["):
            raise ProtocolParseError("fuzzy option frame does not start with '['")
        for line in option_text[1:].splitlines():
            match = re.fullmatch(r"(\d+)\)(.*?)-->(.*)", line)
            if match is None:
                raise ProtocolParseError("fuzzy option line has an unknown shape")
            options.append(
                {
                    "index": int(match.group(1)),
                    "dictionary": match.group(2),
                    "word": match.group(3),
                }
            )
        if not options or [item["index"] for item in options] != list(range(len(options))):
            raise ProtocolParseError("fuzzy option indexes are missing or non-contiguous")
        payload = "[" + selected_text.strip()
        decisions.extend(["fuzzy_options", "choice_prompt", f"choice:{choice}"])
    try:
        entries = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ProtocolParseError("result JSON frame is invalid") from exc
    if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
        raise ProtocolParseError("result JSON frame is not a list of objects")
    decisions.extend(["json_result", "terminal_main_prompt", "stdin_eof"])
    return {"entries": entries, "options": options, "decisions": decisions}


def parse_empty_eof(stdout: bytes) -> dict[str, Any]:
    if stdout != MAIN_PROMPT + b"\n":
        raise ProtocolParseError("top-level EOF produced unexpected output")
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
    evidence_dir: pathlib.Path,
    query: str | None,
    choice: int | None = None,
    close_after_query: bool = False,
    close_on_initial_prompt: bool = False,
    timeout_seconds: float = 5.0,
    cancel_after_seconds: float | None = None,
    stdout_cap_bytes: int = DEFAULT_CAP_BYTES,
    stderr_cap_bytes: int = STDERR_CAP_BYTES,
    inspect_query_cmdline: bool = False,
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
    deadline = time.monotonic() + timeout_seconds
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
            raise RuntimeError("attempted to write after stdin closure")
        try:
            process.stdin.write(payload)
            process.stdin.flush()
        except BrokenPipeError as exc:
            raise RuntimeError("sdcv closed stdin before protocol response") from exc
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

    with (evidence_dir / stdout_name).open("wb") as stdout_handle, (
        evidence_dir / stderr_name
    ).open("wb") as stderr_handle:
        while selector.get_map() or process.poll() is None:
            now = time.monotonic()
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
                    stored = chunk[:remaining]
                    stdout_buffer.extend(stored)
                    stdout_handle.write(stored)
                    while True:
                        position = stdout_buffer.find(MAIN_PROMPT, main_search_from)
                        if position < 0:
                            main_search_from = max(
                                0, len(stdout_buffer) - len(MAIN_PROMPT) + 1
                            )
                            break
                        main_prompts += 1
                        main_search_from = position + len(MAIN_PROMPT)
                    while True:
                        position = stdout_buffer.find(CHOICE_PROMPT, choice_search_from)
                        if position < 0:
                            choice_search_from = max(
                                0, len(stdout_buffer) - len(CHOICE_PROMPT) + 1
                            )
                            break
                        choice_prompts += 1
                        choice_search_from = position + len(CHOICE_PROMPT)
                    if stdout_observed > stdout_cap_bytes:
                        kill("stdout_cap")
                else:
                    stderr_observed += len(chunk)
                    remaining = max(0, stderr_cap_bytes - len(stderr_buffer))
                    stored = chunk[:remaining]
                    stderr_buffer.extend(stored)
                    stderr_handle.write(stored)
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
                        (evidence_dir / cmdline_name).write_bytes(cmdline)
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
    selector.close()
    group_reaped = _process_group_reaped(process.pid)
    duration_ms = round((time.monotonic_ns() - started_ns) / 1_000_000, 3)
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


def run_candidate(
    *,
    name: str,
    argv: list[str],
    stdin_payload: bytes,
    environment: dict[str, str],
    evidence_dir: pathlib.Path,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    started_ns = time.monotonic_ns()
    timed_out = False
    try:
        completed = subprocess.run(
            argv,
            input=stdin_payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            timeout=timeout_seconds,
            check=False,
        )
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = None
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
    duration_ms = round((time.monotonic_ns() - started_ns) / 1_000_000, 3)
    stdout_name = f"{name}.stdout.bin"
    stderr_name = f"{name}.stderr.bin"
    (evidence_dir / stdout_name).write_bytes(stdout)
    (evidence_dir / stderr_name).write_bytes(stderr)
    return {
        "name": name,
        "argv_metadata": argv[1:],
        "stdin_bytes": len(stdin_payload),
        "returncode": returncode,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
        "stdout_bytes": len(stdout),
        "stderr_bytes": len(stderr),
        "stdout_file": stdout_name,
        "stderr_file": stderr_name,
    }


def discover(args: argparse.Namespace) -> int:
    timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    evidence_dir = args.evidence_dir or (
        ROOT / "tests/evidence/sdcv-stdin" / f"discovery-{timestamp}"
    )
    evidence_dir.mkdir(parents=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="dank-sdcv-stdin-") as temporary:
        temp = pathlib.Path(temporary)
        data_root = build_fixture_tree(temp)
        environment = os.environ.copy()
        for private_directory in (temp / "home", temp / "config", temp / "data"):
            private_directory.mkdir(mode=0o700)
        environment.update(
            {
                "HOME": str(temp / "home"),
                "XDG_CONFIG_HOME": str(temp / "config"),
                "XDG_DATA_HOME": str(temp / "data"),
                "SDCV_HISTFILE": "/dev/null",
                "SDCV_HISTSIZE": "0",
                "LC_ALL": "C.UTF-8",
            }
        )
        cases = [
            ("interactive_exact", False, "word\n"),
            ("non_interactive_exact", True, "word\n"),
            ("interactive_miss", False, "definitely-absent\n"),
            ("interactive_fuzzy_choice", False, "/wrod\n0\n"),
            ("interactive_fuzzy_abort", False, "/wrod\n-1\n"),
            ("interactive_eof", False, ""),
        ]
        results = [
            run_candidate(
                name=name,
                argv=sdcv_argv(args.sdcv, data_root, non_interactive=non_interactive),
                stdin_payload=payload.encode("utf-8"),
                environment=environment,
                evidence_dir=evidence_dir,
            )
            for name, non_interactive, payload in cases
        ]
        history_paths = [
            str(path.relative_to(temp))
            for path in temp.rglob("*")
            if path.is_file() and "history" in path.name.casefold()
        ]
    report = {
        "schema_version": 1,
        "kind": "sdcv_stdin_protocol_discovery",
        "sdcv": args.sdcv,
        "stdout_cap_bytes": DEFAULT_CAP_BYTES,
        "history_paths": history_paths,
        "cases": results,
    }
    report_path = evidence_dir / "report.json"
    report_path.write_text(serialize_report(report), encoding="utf-8")
    print(report_path)
    for result in results:
        print(
            f"{result['name']}: rc={result['returncode']} timeout={result['timed_out']} "
            f"stdout={result['stdout_bytes']} stderr={result['stderr_bytes']}"
        )
    return 0


def private_environment(temp: pathlib.Path) -> dict[str, str]:
    for private_directory in (temp / "home", temp / "config", temp / "data"):
        private_directory.mkdir(mode=0o700, exist_ok=True)
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(temp / "home"),
            "XDG_CONFIG_HOME": str(temp / "config"),
            "XDG_DATA_HOME": str(temp / "data"),
            "SDCV_HISTFILE": "/dev/null",
            "SDCV_HISTSIZE": "0",
            "LC_ALL": "C.UTF-8",
        }
    )
    return environment


def parser_summary(parsed: dict[str, Any]) -> dict[str, Any]:
    definitions = [str(item.get("definition", "")) for item in parsed["entries"]]
    return {
        "decisions": parsed["decisions"],
        "entry_count": len(parsed["entries"]),
        "headwords": [str(item.get("word", "")) for item in parsed["entries"]],
        "dictionaries": [str(item.get("dict", "")) for item in parsed["entries"]],
        "definition_chars": [len(value) for value in definitions],
        "definition_sha256": [
            hashlib.sha256(value.encode("utf-8")).hexdigest() for value in definitions
        ],
        "options": parsed["options"],
    }


def normalized_definitions(parsed: dict[str, Any]) -> list[str]:
    return [str(item.get("definition", "")).removeprefix("\n") for item in parsed["entries"]]


def run_completed_gate_case(
    *,
    sdcv: str,
    data_root: pathlib.Path,
    environment: dict[str, str],
    evidence_dir: pathlib.Path,
    name: str,
    query: str,
    dictionaries: list[str],
    exact: bool,
    expected_definitions: list[str],
    choice: int | None = None,
    expected_headwords: list[str] | None = None,
    close_after_query: bool = False,
    inspect_query_cmdline: bool = False,
    stdout_cap_bytes: int = DEFAULT_CAP_BYTES,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    result, stdout = production_run_bounded_protocol(
        name=name,
        argv=production_protocol_argv(
            sdcv,
            data_dir=str(data_root),
            dictionaries=dictionaries,
            exact=exact,
        ),
        environment=environment,
        evidence_dir=evidence_dir,
        query=query,
        choice=choice,
        close_after_query=close_after_query,
        timeout_seconds=8.0,
        stdout_cap_bytes=stdout_cap_bytes,
        inspect_query_cmdline=inspect_query_cmdline,
    )
    errors: list[str] = []
    parsed: dict[str, Any] | None = None
    if result["termination_reason"] is not None:
        errors.append(f"unexpected termination: {result['termination_reason']}")
    if result["returncode"] != 0:
        errors.append(f"unexpected return code: {result['returncode']}")
    if result["stderr_bytes"] != 0:
        errors.append(f"unexpected stderr bytes: {result['stderr_bytes']}")
    if not result["process_group_reaped"]:
        errors.append("process group was not fully reaped")
    if query in result["argv_metadata"]:
        errors.append("query appeared as an argv element")
    try:
        parsed = production_parse_protocol_stdout(stdout, query=query, choice=choice)
    except ProductionProtocolError as exc:
        errors.append(str(exc))
    if parsed is not None:
        actual_definitions = normalized_definitions(parsed)
        if actual_definitions != expected_definitions:
            errors.append("definitions did not match isolated expected results")
        if expected_headwords is not None:
            actual_headwords = [
                str(item.get("word", "")) for item in parsed["entries"]
            ]
            if actual_headwords != expected_headwords:
                errors.append("headwords did not match isolated expected results")
        result["parser"] = parser_summary(parsed)
    if inspect_query_cmdline:
        if result["query_in_cmdline"]:
            errors.append("sentinel query appeared in sdcv cmdline")
        if not (evidence_dir / str(result["cmdline_file"])).read_bytes():
            errors.append("sdcv cmdline could not be captured")
    result["errors"] = errors
    result["status"] = "pass" if not errors else "fail"
    return result, parsed


def finalize_fuzzy_case(
    result: dict[str, Any],
    parsed: dict[str, Any] | None,
    expected_headword: str,
) -> dict[str, Any]:
    if parsed is not None and not any(
        item["word"] == expected_headword for item in parsed["options"]
    ):
        result["errors"].append("expected fuzzy candidate was absent")
        result["status"] = "fail"
    return result


def finalize_empty_eof_case(
    result: dict[str, Any], stdout: bytes
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        parsed = production_parse_empty_eof(stdout)
        result["parser"] = parser_summary(parsed)
    except ProductionProtocolError as exc:
        errors.append(str(exc))
    if result["returncode"] != 0 or result["termination_reason"] is not None:
        errors.append("top-level EOF did not exit cleanly")
    if b"Invalid choice" in stdout:
        errors.append("top-level EOF entered Invalid choice loop")
    if not result["process_group_reaped"]:
        errors.append("top-level EOF process group was not reaped")
    result["errors"] = errors
    result["status"] = "pass" if not errors else "fail"
    return result


def finalize_timeout_case(
    result: dict[str, Any], timeout_seconds: float
) -> dict[str, Any]:
    errors: list[str] = []
    if result["termination_reason"] != "timeout":
        errors.append("timeout did not terminate at its configured bound")
    if result["duration_ms"] > (timeout_seconds + 1.0) * 1000:
        errors.append("timeout completion exceeded its upper bound")
    if not result["process_group_reaped"]:
        errors.append("timed-out process group was not reaped")
    result["errors"] = errors
    result["status"] = "pass" if not errors else "fail"
    return result


def finalize_cancellation_case(result: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    if result["termination_reason"] != "cancelled":
        errors.append("cancellation did not terminate the running process")
    if result["duration_ms"] > 1050:
        errors.append("cancellation completion exceeded its upper bound")
    if not result["process_group_reaped"]:
        errors.append("cancelled process group was not reaped")
    if "query_sent" not in result.get("runner_decisions", []):
        errors.append("cancellation occurred before the lookup query was sent")
    result["errors"] = errors
    result["status"] = "pass" if not errors else "fail"
    return result


def finalize_giant_under_case(
    result: dict[str, Any], parsed: dict[str, Any] | None
) -> dict[str, Any]:
    if parsed is not None:
        definitions = normalized_definitions(parsed)
        result["errors"] = [
            error
            for error in result["errors"]
            if error != "definitions did not match isolated expected results"
        ]
        if (
            len(definitions) != 1
            or len(definitions[0].encode("utf-8")) != GIANT_UNDER_BYTES
        ):
            result["errors"].append("32 MiB definition length was not preserved")
        elif not definitions[0].endswith("GIANT-32-COMPLETE-TAIL"):
            result["errors"].append("32 MiB definition tail was not preserved")
        if result["stdout_bytes"] >= DEFAULT_CAP_BYTES:
            result["errors"].append("32 MiB case did not remain below stdout cap")
        result["status"] = "pass" if not result["errors"] else "fail"
    return result


def finalize_stdout_over_cap_case(result: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    if result["termination_reason"] != "stdout_cap":
        errors.append("over-cap output was not terminated at the stdout cap")
    if result["stdout_stored_bytes"] != DEFAULT_CAP_BYTES:
        errors.append("stored over-cap evidence did not stop at exactly the cap")
    if not result["process_group_reaped"]:
        errors.append("over-cap process group was not reaped")
    result["errors"] = errors
    result["status"] = "pass" if not errors else "fail"
    return result


def finalize_stderr_over_cap_case(result: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    if result["termination_reason"] != "stderr_cap":
        errors.append("over-cap stderr was not terminated at the stderr cap")
    if result["stderr_stored_bytes"] != STDERR_CAP_BYTES:
        errors.append("stored stderr evidence did not stop at exactly the cap")
    if not (STDERR_CAP_BYTES < result["stderr_bytes"] <= STDERR_CAP_BYTES + 1):
        errors.append("observed stderr bytes did not stop at cap plus sentinel")
    if not result["process_group_reaped"]:
        errors.append("stderr-capped process group was not reaped")
    result["errors"] = errors
    result["status"] = "pass" if not errors else "fail"
    return result


def finalize_sentinel_case(
    result: dict[str, Any],
    sentinel: str,
    environment: dict[str, str],
) -> dict[str, Any]:
    result["sentinel_environment_match"] = any(
        sentinel in value for value in environment.values()
    )
    if result["sentinel_environment_match"]:
        result["errors"].append("sentinel query appeared in environment")
        result["status"] = "fail"
    return result


def gate(args: argparse.Namespace) -> int:
    timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    evidence_dir = args.evidence_dir or (
        ROOT / "tests/evidence/sdcv-stdin" / f"gate-{timestamp}"
    )
    evidence_dir.mkdir(parents=True, mode=0o700)
    expected = load_json(FIXTURE_ROOT / "expected.json")
    cases: list[dict[str, Any]] = []

    def record_completed(
        *,
        name: str,
        query: str,
        dictionaries: list[str],
        exact: bool,
        expected_definitions: list[str],
        choice: int | None = None,
        expected_headwords: list[str] | None = None,
        close_after_query: bool = False,
        inspect_query_cmdline: bool = False,
        stdout_cap_bytes: int = DEFAULT_CAP_BYTES,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        result, parsed = run_completed_gate_case(
            sdcv=args.sdcv,
            data_root=data_root,
            environment=environment,
            evidence_dir=evidence_dir,
            name=name,
            query=query,
            dictionaries=dictionaries,
            exact=exact,
            expected_definitions=expected_definitions,
            choice=choice,
            expected_headwords=expected_headwords,
            close_after_query=close_after_query,
            inspect_query_cmdline=inspect_query_cmdline,
            stdout_cap_bytes=stdout_cap_bytes,
        )
        cases.append(result)
        return result, parsed

    with tempfile.TemporaryDirectory(prefix="dank-sdcv-stdin-") as temporary:
        temp = pathlib.Path(temporary)
        data_root = build_fixture_tree(temp, include_giant=True)
        environment = private_environment(temp)

        record_completed(
            name="exact_hit",
            query="word",
            dictionaries=["DANK_STDIN_A"],
            exact=True,
            expected_definitions=expected["exact"]["word"],
            expected_headwords=["word"],
        )
        record_completed(
            name="exact_miss",
            query=expected["exact_miss"],
            dictionaries=["DANK_STDIN_A"],
            exact=True,
            expected_definitions=[],
            expected_headwords=[],
        )
        fuzzy_result, fuzzy_parsed = record_completed(
            name="fuzzy_choice",
            query=expected["fuzzy_query"],
            dictionaries=["DANK_STDIN_A"],
            exact=False,
            choice=0,
            expected_definitions=expected["exact"]["word"],
            expected_headwords=[expected["fuzzy_expected_headword"]],
        )
        finalize_fuzzy_case(
            fuzzy_result, fuzzy_parsed, expected["fuzzy_expected_headword"]
        )

        record_completed(
            name="single_dictionary",
            query="word",
            dictionaries=["DANK_STDIN_B"],
            exact=True,
            expected_definitions=["fixture-b exact english"],
            expected_headwords=["word"],
        )
        record_completed(
            name="multiple_dictionaries",
            query="word",
            dictionaries=["DANK_STDIN_A", "DANK_STDIN_B"],
            exact=True,
            expected_definitions=expected["multi_dictionary_word"],
            expected_headwords=["word", "word"],
        )
        for name, query in (
            ("unicode_en", "word"),
            ("unicode_han", "漢字"),
            ("unicode_ja", "かな"),
            ("unicode_yue", "粵語"),
        ):
            record_completed(
                name=name,
                query=query,
                dictionaries=["DANK_STDIN_A"],
                exact=True,
                expected_definitions=expected["exact"][query],
                expected_headwords=[query],
            )

        record_completed(
            name="stdin_eof_after_exact",
            query="word",
            dictionaries=["DANK_STDIN_A"],
            exact=True,
            close_after_query=True,
            expected_definitions=expected["exact"]["word"],
            expected_headwords=["word"],
        )

        empty_result, empty_stdout = production_run_bounded_protocol(
            name="stdin_eof_at_main_prompt",
            argv=production_protocol_argv(
                args.sdcv,
                data_dir=str(data_root),
                dictionaries=["DANK_STDIN_A"],
                exact=True,
            ),
            environment=environment,
            evidence_dir=evidence_dir,
            query=None,
            close_on_initial_prompt=True,
            timeout_seconds=2.0,
        )
        finalize_empty_eof_case(empty_result, empty_stdout)
        cases.append(empty_result)

        for name, timeout_seconds in (("source_timeout", 0.10), ("overall_timeout", 0.25)):
            timeout_result, _stdout = production_run_bounded_protocol(
                name=name,
                argv=production_protocol_argv(
                    args.sdcv,
                    data_dir=str(data_root),
                    dictionaries=["DANK_STDIN_A"],
                    exact=True,
                ),
                environment=environment,
                evidence_dir=evidence_dir,
                query=None,
                timeout_seconds=timeout_seconds,
            )
            finalize_timeout_case(timeout_result, timeout_seconds)
            cases.append(timeout_result)

        cancel_result, _stdout = production_run_bounded_protocol(
            name="cancellation",
            argv=production_protocol_argv(
                args.sdcv,
                data_dir=str(data_root),
                dictionaries=[GIANT_UNDER_BOOK],
                exact=True,
            ),
            environment=environment,
            evidence_dir=evidence_dir,
            query="giant-under-cap",
            timeout_seconds=2.0,
            cancel_after_seconds=0.05,
        )
        finalize_cancellation_case(cancel_result)
        cases.append(cancel_result)

        giant_result, giant_parsed = record_completed(
            name="stdout_under_cap_32mib",
            query="giant-under-cap",
            dictionaries=[GIANT_UNDER_BOOK],
            exact=True,
            expected_definitions=[],
            expected_headwords=["giant-under-cap"],
        )
        finalize_giant_under_case(giant_result, giant_parsed)

        over_result, _stdout = production_run_bounded_protocol(
            name="stdout_over_cap",
            argv=production_protocol_argv(
                args.sdcv,
                data_dir=str(data_root),
                dictionaries=[GIANT_OVER_BOOK],
                exact=True,
            ),
            environment=environment,
            evidence_dir=evidence_dir,
            query="giant-over-cap",
            timeout_seconds=8.0,
            stdout_cap_bytes=DEFAULT_CAP_BYTES,
        )
        finalize_stdout_over_cap_case(over_result)
        cases.append(over_result)

        stderr_producer = temp / "sustained-stderr.py"
        stderr_producer.write_text(
            "import os\n"
            "chunk = b'x' * (64 * 1024)\n"
            "while True:\n"
            "    os.write(2, chunk)\n",
            encoding="utf-8",
        )
        stderr_result, _stdout = production_run_bounded_protocol(
            name="stderr_over_cap",
            argv=[sys.executable, str(stderr_producer)],
            environment=environment,
            evidence_dir=evidence_dir,
            query=None,
            timeout_seconds=2.0,
            stdout_cap_bytes=DEFAULT_CAP_BYTES,
            stderr_cap_bytes=STDERR_CAP_BYTES,
        )
        finalize_stderr_over_cap_case(stderr_result)
        cases.append(stderr_result)

        sentinel = "cmdline-" + secrets.token_hex(24)
        sentinel_result, _parsed = record_completed(
            name="sentinel_cmdline",
            query=sentinel,
            dictionaries=["DANK_STDIN_A"],
            exact=True,
            expected_definitions=[],
            expected_headwords=[],
            inspect_query_cmdline=True,
        )
        finalize_sentinel_case(sentinel_result, sentinel, environment)

        history_paths = [
            str(path.relative_to(temp))
            for path in temp.rglob("*")
            if path.is_file() and "history" in path.name.casefold()
        ]

    version = subprocess.run(
        [args.sdcv, "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        timeout=2.0,
    )
    version_raw = version.stdout.strip()
    report = build_gate_report(
        version_returncode=version.returncode,
        version_raw=version_raw,
        history_paths=history_paths,
        cases=cases,
    )
    report_path = evidence_dir / "report.json"
    report_path.write_text(serialize_report(report), encoding="utf-8")
    print(report_path)
    for case in cases:
        print(
            f"{case['name']}: {case['status']} rc={case['returncode']} "
            f"termination={case['termination_reason']} stdout={case['stdout_bytes']} "
            f"stderr={case['stderr_bytes']}"
        )
    print(f"SDCV_STDIN_GATE: {report['verdict'].upper()}")
    return 0 if report["verdict"] == "pass" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdcv", default="sdcv")
    parser.add_argument("--evidence-dir", type=pathlib.Path)
    parser.add_argument(
        "--discovery",
        action="store_true",
        help="record the initial candidate protocol matrix instead of running the gate",
    )
    args = parser.parse_args(argv)
    return discover(args) if args.discovery else gate(args)


if __name__ == "__main__":
    raise SystemExit(main())
