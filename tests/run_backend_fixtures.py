#!/usr/bin/env python3
import json, subprocess, sys, pathlib, os

ROOT = pathlib.Path.home() / ".local/lib/dank-lookup"
BACKEND = ROOT / "backends/sdcv.sh"
DEFAULT_FIXTURES = ROOT / "tests/fixtures"
TMP = pathlib.Path("/tmp/dank-lookup")

passed = 0
failed = 0

def get_at(data, dotted_key):
    parts = dotted_key.split(".", 1)
    if len(parts) == 1:
        return data.get(parts[0]) if isinstance(data, dict) else None
    sub = data.get(parts[0], {}) if isinstance(data, dict) else {}
    return get_at(sub, parts[1]) if isinstance(sub, dict) else None

def check_contains(label, actual, needles, errors):
    text = actual if isinstance(actual, str) else ""
    missing = [n for n in needles if n not in text]
    if missing:
        errors.append(f"{label}: missing {missing}")

def check_contains_any(label, actual, needles, errors):
    text = actual if isinstance(actual, str) else ""
    if not any(n in text for n in needles):
        errors.append(f"{label}: none of {needles} found")

def check_not_contains(label, actual, needles, errors):
    text = actual if isinstance(actual, str) else ""
    found = [n for n in needles if n in text]
    if found:
        errors.append(f"{label}: contains {found}")

def check_expect(data, expect, prefix="errors"):
    errors = []
    for key, val in expect.items():
        label = f"{prefix}.{key}"

        if key == "backend":
            if data.get("backend") != val:
                errors.append(f"{label}: expected {val!r}, got {data.get('backend')!r}")

        elif key == "default_mode":
            if data.get("default_mode") != val:
                errors.append(f"{label}: expected {val!r}, got {data.get('default_mode')!r}")

        elif key == "default_mode_not":
            if data.get("default_mode") == val:
                errors.append(f"{label}: should not be {val!r}")

        elif key == "available_modes_contains":
            actual = data.get("available_modes", [])
            if not isinstance(actual, list):
                errors.append(f"{label}: not a list ({type(actual).__name__})")
            else:
                missing = [m for m in val if m not in actual]
                if missing:
                    errors.append(f"{label}: missing modes {missing} (have {actual})")

        elif key == "available_modes_not_contains":
            actual = data.get("available_modes", [])
            if isinstance(actual, list):
                found = [m for m in val if m in actual]
                if found:
                    errors.append(f"{label}: should not contain {found}")

        elif val is None:
            if get_at(data, key) is not None:
                errors.append(f"{label}: expected null, got {get_at(data, key)!r}")

        elif isinstance(val, dict):
            sub = get_at(data, key) or {}
            if not isinstance(sub, dict):
                errors.append(f"{label}: not a dict ({type(sub).__name__})")
                continue
            for check_key, check_val in val.items():
                clabel = f"{label}.{check_key}"
                if check_key == "subtitle_contains":
                    check_contains(clabel, sub.get("subtitle", ""), check_val, errors)
                elif check_key == "body_contains":
                    check_contains(clabel, sub.get("body", ""), check_val, errors)
                elif check_key == "body_contains_any":
                    check_contains_any(clabel, sub.get("body", ""), check_val, errors)
                elif check_key == "body_not_contains":
                    check_not_contains(clabel, sub.get("body", ""), check_val, errors)

        else:
            actual = get_at(data, key)
            if actual != val:
                errors.append(f"{label}: expected {val!r}, got {actual!r}")

    return errors

def run_fixture(path):
    global passed, failed
    fixture = json.loads(path.read_text(encoding="utf-8"))
    query = fixture["query"]
    expect = fixture["expect"]

    out_path = TMP / f"fixture-{path.stem}.json"
    env = os.environ.copy()
    env["DANK_LOOKUP_OUTPUT"] = str(out_path)

    result = subprocess.run([str(BACKEND), query], capture_output=True, text=True, timeout=10, env=env)
    if result.returncode != 0:
        print(f"  FAIL  {query}: backend exit {result.returncode}")
        failed += 1
        return

    try:
        data = json.loads(out_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  FAIL  {query}: JSON parse error: {e}")
        failed += 1
        return

    errors = check_expect(data, expect, prefix=query)
    if errors:
        print(f"  FAIL  {query}")
        for e in errors:
            print(f"         {e}")
        failed += 1
    else:
        print(f"  PASS  {query}")
        passed += 1

def main():
    global passed, failed
    config_label = os.environ.get("DANK_LOOKUP_CONFIG", "(default)")
    print(f"dankLookup backend fixtures")
    print(f"config: {config_label}")

    fixture_dir = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_FIXTURES
    fixture_files = sorted(fixture_dir.glob("*.json"))
    if not fixture_files:
        print(f"  ERROR: no fixture files found in {fixture_dir}")
        sys.exit(1)
    for f in fixture_files:
        run_fixture(f)
    total = passed + failed
    print(f"\n{'-'*40}")
    print(f"  {passed}/{total} passed")
    if failed:
        sys.exit(1)

if __name__ == "__main__":
    main()
