#!/usr/bin/env python3
"""Run one explicitly configured backend fixture set from this checkout."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_BACKEND = ROOT / "lib/backends/sdcv.sh"
DEFAULT_FIXTURES = ROOT / "tests/fixtures"
CONFIG_BY_SET = {
    "fixtures": ROOT / "tests/configs/legacy-full.json",
    "fixtures-ci-minimal": ROOT / "tests/configs/ci-minimal.json",
    "fixtures-reading-routing": ROOT / "tests/configs/reading-routing.json",
    "fixtures-ja-yue": ROOT / "tests/configs/reading-routing.json",
    "fixtures-risk-hardening": ROOT / "tests/configs/reading-routing.json",
    "fixtures-segmentation": ROOT / "tests/configs/reading-routing.json",
    "fixtures-v2-related": ROOT / "tests/configs/v2-related.json",
}


def get_at(data, dotted_key):
    parts = dotted_key.split(".", 1)
    if len(parts) == 1:
        return data.get(parts[0]) if isinstance(data, dict) else None
    sub = data.get(parts[0], {}) if isinstance(data, dict) else {}
    return get_at(sub, parts[1]) if isinstance(sub, dict) else None


def check_contains(label, actual, needles, errors):
    text = actual if isinstance(actual, str) else ""
    missing = [needle for needle in needles if needle not in text]
    if missing:
        errors.append(f"{label}: missing {missing}")


def check_contains_any(label, actual, needles, errors):
    text = actual if isinstance(actual, str) else ""
    if not any(needle in text for needle in needles):
        errors.append(f"{label}: none of {needles} found")


def check_not_contains(label, actual, needles, errors):
    text = actual if isinstance(actual, str) else ""
    found = [needle for needle in needles if needle in text]
    if found:
        errors.append(f"{label}: contains {found}")


def check_expect(data, expect, prefix="errors"):
    errors = []
    for key, value in expect.items():
        label = f"{prefix}.{key}"
        if key == "backend":
            if data.get("backend") != value:
                errors.append(f"{label}: expected {value!r}, got {data.get('backend')!r}")
        elif key == "default_mode":
            if data.get("default_mode") != value:
                errors.append(f"{label}: expected {value!r}, got {data.get('default_mode')!r}")
        elif key == "default_mode_not":
            if data.get("default_mode") == value:
                errors.append(f"{label}: should not be {value!r}")
        elif key == "available_modes_contains":
            actual = data.get("available_modes", [])
            missing = [mode for mode in value if mode not in actual] if isinstance(actual, list) else value
            if missing:
                errors.append(f"{label}: missing modes {missing} (have {actual})")
        elif key == "available_modes_not_contains":
            actual = data.get("available_modes", [])
            found = [mode for mode in value if mode in actual] if isinstance(actual, list) else []
            if found:
                errors.append(f"{label}: should not contain {found}")
        elif key == "tokens_contains":
            actual = data.get("tokens", [])
            missing = [token for token in value if token not in actual] if isinstance(actual, list) else value
            if missing:
                errors.append(f"{label}: missing tokens {missing} (have {actual})")
        elif key == "tokens_length_gte":
            actual = data.get("tokens", [])
            if not isinstance(actual, list) or len(actual) < value:
                errors.append(f"{label}: expected >= {value} tokens")
        elif key == "token_results_mode_contains":
            token_results = data.get("results_by_token", {})
            modes = {
                mode
                for token_result in token_results.values()
                if isinstance(token_result, dict)
                for mode in token_result.get("available_modes", [])
            }
            missing = [mode for mode in value if mode not in modes]
            if missing:
                errors.append(f"{label}: missing token-result modes {missing} (have {sorted(modes)})")
        elif value is None:
            if get_at(data, key) is not None:
                errors.append(f"{label}: expected null, got {get_at(data, key)!r}")
        elif isinstance(value, dict):
            sub = get_at(data, key) or {}
            if not isinstance(sub, dict):
                errors.append(f"{label}: not a dict ({type(sub).__name__})")
                continue
            for check_key, check_value in value.items():
                check_label = f"{label}.{check_key}"
                if check_key == "subtitle_contains":
                    check_contains(check_label, sub.get("subtitle", ""), check_value, errors)
                elif check_key == "body_contains":
                    check_contains(check_label, sub.get("body", ""), check_value, errors)
                elif check_key == "body_contains_any":
                    check_contains_any(check_label, sub.get("body", ""), check_value, errors)
                elif check_key == "body_not_contains":
                    check_not_contains(check_label, sub.get("body", ""), check_value, errors)
        else:
            actual = get_at(data, key)
            if actual != value:
                errors.append(f"{label}: expected {value!r}, got {actual!r}")
    return errors


def backend_argv(backend: pathlib.Path) -> list[str]:
    if backend.suffix == ".sh":
        return ["bash", str(backend), "--stdin"]
    if os.access(backend, os.X_OK):
        return [str(backend), "--stdin"]
    return [sys.executable, str(backend), "--stdin"]


def config_matches(config_data, when):
    """Whether a fixture's `expect_by_config[].when` holds for a config.

    `version` compares the config schema version (absent means 1);
    `mode_sources_include` maps a mode id to dictionary names that mode must
    list. Conditions describe the config, never the lookup result.
    """
    unknown = set(when) - {"version", "mode_sources_include"}
    if unknown:
        raise ValueError(f"unknown expect_by_config condition {sorted(unknown)}")
    if "version" in when and config_data.get("version", 1) != when["version"]:
        return False
    modes = config_data.get("modes", {})
    for mode, names in when.get("mode_sources_include", {}).items():
        mode_config = modes.get(mode)
        listed = {
            str(source.get("name", ""))
            for source in (mode_config or {}).get("sources", [])
            if isinstance(source, dict)
        }
        if not isinstance(mode_config, dict) or not set(names) <= listed:
            return False
    return True


def fixture_expectations(fixture, config_data):
    """Return the fixture's base expectations with matching config variants applied."""
    expect = dict(fixture["expect"])
    applied = []
    for variant in fixture.get("expect_by_config", []):
        if not config_matches(config_data, variant["when"]):
            continue
        if not variant.get("reason"):
            raise ValueError("expect_by_config entries must state a reason")
        for key, value in variant["expect"].items():
            if isinstance(value, dict) and isinstance(expect.get(key), dict):
                expect[key] = {**expect[key], **value}
            else:
                expect[key] = value
        applied.append(variant["reason"])
    return expect, applied


def run_fixture(path, backend, config, output_dir, timeout):
    fixture = json.loads(path.read_text(encoding="utf-8"))
    query = fixture["query"]
    config_data = json.loads(pathlib.Path(config).read_text(encoding="utf-8"))
    expect, applied = fixture_expectations(fixture, config_data)
    output = output_dir / f"fixture-{path.stem}.json"
    env = os.environ.copy()
    env["DANK_LOOKUP_OUTPUT"] = str(output)
    env["DANK_LOOKUP_CONFIG"] = str(config)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        backend_argv(backend),
        input=query,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if result.returncode != 0:
        return [f"backend exit {result.returncode}: {result.stderr.strip()}"], applied
    try:
        data = json.loads(output.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"JSON parse error: {exc}"], applied
    return check_expect(data, expect, prefix=query), applied


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixtures", nargs="?", type=pathlib.Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--backend", type=pathlib.Path, default=pathlib.Path(os.environ.get("DANK_LOOKUP_BACKEND", DEFAULT_BACKEND)))
    parser.add_argument("--config", type=pathlib.Path)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)

    fixture_dir = args.fixtures.resolve()
    backend = args.backend.resolve()
    if args.config is not None:
        config = args.config
    elif os.environ.get("DANK_LOOKUP_CONFIG"):
        config = pathlib.Path(os.environ["DANK_LOOKUP_CONFIG"])
    else:
        config = CONFIG_BY_SET.get(fixture_dir.name)
    if config is None:
        parser.error(f"no exact config mapped for fixture set {fixture_dir.name}; pass --config")
    config = pathlib.Path(config).resolve()
    if not backend.is_file():
        parser.error(f"backend not found: {backend}")
    if not config.is_file():
        parser.error(f"config not found: {config}")
    fixture_files = sorted(fixture_dir.glob("*.json"))
    if not fixture_files:
        parser.error(f"no fixture files found in {fixture_dir}")

    print("Dank Lookup backend fixture set")
    print(f"fixtures: {fixture_dir}")
    print(f"backend:  {backend}")
    print(f"config:   {config}")
    passed = 0
    failed = 0
    with tempfile.TemporaryDirectory(prefix="dank-lookup-fixtures-") as temporary:
        output_dir = pathlib.Path(temporary)
        for fixture in fixture_files:
            errors, applied = run_fixture(fixture, backend, config, output_dir, args.timeout)
            if errors:
                print(f"  FAIL  {fixture.name}")
                for error in errors:
                    print(f"         {error}")
                failed += 1
            else:
                print(f"  PASS  {fixture.name}")
                passed += 1
            for reason in applied:
                print(f"         config variant: {reason}")
    print(f"result: {passed}/{passed + failed} passed for {fixture_dir.name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
