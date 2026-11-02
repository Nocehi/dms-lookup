#!/usr/bin/env python3
"""Prove the multi-frame sdcv session against the installed sdcv 0.5.5.

The stdin gate (``probe_sdcv_stdin.py``) proves one query per process. This
probe proves what ``SdcvSession`` relies on beyond that, using the same
isolated benign dictionaries:

* one process answers an exact query, a ``/fuzzy`` listing with a choice, a
  candidate re-query and a miss in sequence, then exits cleanly on stdin EOF;
* ``/fuzzy`` still lists candidates when the process runs with
  ``--exact-search`` (so one session could serve both search kinds);
* an exact homograph prompt can be aborted with ``-1`` and then answered
  with each index inside the same process.

Queries are written only to the child's stdin. The report is JSON on stdout
plus one line per case; exit status is non-zero when any case fails.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from dank_lookup_sdcv_stdin import (  # noqa: E402
    SdcvProtocolError,
    SdcvSession,
    parse_frame,
    protocol_argv,
)
from probe_sdcv_stdin import (  # noqa: E402
    build_dictionary,
    build_fixture_tree,
    load_json,
    private_environment,
    FIXTURE_ROOT,
)

HOMOGRAPH_BOOK = "DANK_SESSION_HOMO"


def run_session_case(
    *,
    name: str,
    sdcv: str,
    data_root: pathlib.Path,
    environment: dict[str, str],
    dictionaries: list[str],
    exact: bool,
    steps: list[dict[str, Any]],
) -> dict[str, Any]:
    """Run ``steps`` (query/choice/expectations) through one session."""
    session = SdcvSession(
        name=name,
        argv=protocol_argv(sdcv, data_dir=str(data_root), dictionaries=dictionaries, exact=exact),
        environment=environment,
    )
    errors: list[str] = []
    frames: list[dict[str, Any]] = []
    for step in steps:
        query = str(step["query"])
        choice = step.get("choice")
        run, frame = session.ask(query, choice=choice, timeout_seconds=8.0)
        record: dict[str, Any] = {
            "query_bytes": run["query_bytes"],
            "choice": choice,
            "pid": run["pid"],
            "session_start": run["session_start"],
            "termination_reason": run["termination_reason"],
            "stdout_bytes": run["stdout_bytes"],
            "choice_prompt_count": run["choice_prompt_count"],
            "choice_aborted": run["choice_aborted"],
            "session_alive": run["session_alive"],
            "runner_decisions": run["runner_decisions"],
        }
        if run["termination_reason"] is not None:
            errors.append(f"{query!r}: unexpected termination {run['termination_reason']}")
        if run["session_restarted"]:
            errors.append(f"{query!r}: session restarted a process")
        parser_choice = choice if run["choice_prompt_count"] and choice is not None else None
        if parser_choice is None and run["choice_aborted"]:
            parser_choice = -1
        try:
            parsed = parse_frame(frame, query=query, choice=parser_choice)
        except SdcvProtocolError as exc:
            errors.append(f"{query!r}: {exc}")
            frames.append(record)
            continue
        headwords = [str(item.get("word", "")) for item in parsed["entries"]]
        definitions = [str(item.get("definition", "")).removeprefix("\n") for item in parsed["entries"]]
        options = [str(item["word"]) for item in parsed["options"]]
        record.update({"headwords": headwords, "definitions": definitions, "options": options})
        if "expected_definitions" in step and definitions != step["expected_definitions"]:
            errors.append(f"{query!r}: definitions {definitions!r} != {step['expected_definitions']!r}")
        if "expected_headwords" in step and headwords != step["expected_headwords"]:
            errors.append(f"{query!r}: headwords {headwords!r} != {step['expected_headwords']!r}")
        for expected_option in step.get("expected_options", []):
            if expected_option not in options:
                errors.append(f"{query!r}: candidate {expected_option!r} absent from {options!r}")
        if step.get("expect_choice_prompt") and not run["choice_prompt_count"]:
            errors.append(f"{query!r}: choice prompt did not appear")
        if step.get("expect_choice_aborted") and not run["choice_aborted"]:
            errors.append(f"{query!r}: choice prompt was not aborted")
        frames.append(record)
    closed = session.close()
    if closed["returncode"] != 0:
        errors.append(f"close: return code {closed['returncode']}")
    if closed["termination_reason"] is not None:
        errors.append(f"close: termination {closed['termination_reason']}")
    if not closed["process_group_reaped"]:
        errors.append("close: process group was not reaped")
    if closed["session_starts"] != 1:
        errors.append(f"close: {closed['session_starts']} processes were started")
    return {
        "name": name,
        "argv_metadata": session.argv[1:],
        "frames": frames,
        "close": closed,
        "errors": errors,
        "status": "pass" if not errors else "fail",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdcv", default="sdcv")
    parser.add_argument("--output", type=pathlib.Path, help="also write the JSON report here")
    args = parser.parse_args(argv)
    expected = load_json(FIXTURE_ROOT / "expected.json")
    cases: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="dank-sdcv-session-") as temporary:
        temp = pathlib.Path(temporary)
        data_root = build_fixture_tree(temp)
        build_dictionary(
            data_root / "dic",
            {
                "bookname": HOMOGRAPH_BOOK,
                "sametypesequence": "m",
                "entries": [
                    {"word": "dup", "definition": "homograph one"},
                    {"word": "dup", "definition": "homograph two"},
                    {"word": "other", "definition": "not a homograph"},
                ],
            },
        )
        environment = private_environment(temp)
        common = {"sdcv": args.sdcv, "data_root": data_root, "environment": environment}

        cases.append(
            run_session_case(
                name="session_exact_fuzzy_candidate_miss",
                dictionaries=["DANK_STDIN_A"],
                exact=False,
                steps=[
                    {"query": "word", "expected_definitions": expected["exact"]["word"], "expected_headwords": ["word"]},
                    {
                        "query": expected["fuzzy_query"],
                        "choice": 0,
                        "expect_choice_prompt": True,
                        "expected_options": [expected["fuzzy_expected_headword"]],
                    },
                    {"query": "world", "expected_definitions": ["fixture-a related english"]},
                    {"query": "漢字", "expected_definitions": expected["exact"]["漢字"]},
                ],
                **common,
            )
        )
        cases.append(
            run_session_case(
                name="session_fuzzy_under_exact_search",
                dictionaries=["DANK_STDIN_A"],
                exact=True,
                steps=[
                    {"query": expected["exact_miss"], "expected_definitions": [], "expected_headwords": []},
                    {
                        "query": expected["fuzzy_query"],
                        "choice": 0,
                        "expect_choice_prompt": True,
                        "expected_options": [expected["fuzzy_expected_headword"]],
                    },
                    {"query": "word", "expected_definitions": expected["exact"]["word"]},
                ],
                **common,
            )
        )
        cases.append(
            run_session_case(
                name="session_homograph_abort_then_each_index",
                dictionaries=[HOMOGRAPH_BOOK],
                exact=True,
                steps=[
                    {"query": "dup", "expect_choice_prompt": True, "expect_choice_aborted": True, "expected_headwords": []},
                    {"query": "dup", "choice": 0, "expect_choice_prompt": True, "expected_definitions": ["homograph one"]},
                    {"query": "dup", "choice": 1, "expect_choice_prompt": True, "expected_definitions": ["homograph two"]},
                    {"query": "other", "expected_definitions": ["not a homograph"]},
                ],
                **common,
            )
        )
        history_paths = [
            str(path.relative_to(temp))
            for path in temp.rglob("*")
            if path.is_file() and "history" in path.name.casefold()
        ]
    version = subprocess.run(
        [args.sdcv, "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False, timeout=2.0
    )
    verdict = "pass" if all(case["status"] == "pass" for case in cases) and not history_paths else "fail"
    report = {
        "kind": "sdcv_0_5_5_session_probe",
        "verdict": verdict,
        "sdcv_version_raw": version.stdout.strip(),
        "history_paths": history_paths,
        "cases": cases,
    }
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    sys.stdout.write(serialized)
    for case in cases:
        print(f"{case['name']}: {case['status']} frames={len(case['frames'])} errors={case['errors']}", file=sys.stderr)
    print(f"SDCV_SESSION_PROBE: {verdict.upper()}", file=sys.stderr)
    return 0 if verdict == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
