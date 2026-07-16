#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
REAL_PYTHON="$(command -v python3)"
cd "$REPO_ROOT"

# shellcheck disable=SC1091
source packaging/pre-release-check.sh

case_roots=("$TEMP_ROOT")
cleanup() {
    rm -rf -- "${case_roots[@]}"
}
trap cleanup EXIT

injection_case=''
injection_target=''
python3() {
    if [[ "${1-}" == "-" && "${2-}" == "$injection_target" ]]; then
        printf '%s\n' \
            "INJECTED_INSTALL_PARITY_FAILURE case=$injection_case exit=42 target=${2-}" \
            >&2
        return 42
    fi
    "$REAL_PYTHON" "$@"
}

assert_absent() {
    local path="$1"
    if [[ -e "$path" || -L "$path" ]]; then
        printf '%s\n' \
            "later fail-fast artifact exists case=$injection_case path=$path" >&2
        return 1
    fi
}

run_injection_case() {
    injection_case="$1"
    TEMP_ROOT="$(mktemp -d)"
    case_roots+=("$TEMP_ROOT")
    failures=0

    local later_paths=()
    case "$injection_case" in
        early)
            injection_target="$TEMP_ROOT/config/dank-lookup/config.json"
            later_paths=(
                "$TEMP_ROOT/install-repeat.log"
                "$TEMP_ROOT/installed-cli.json"
                "$TEMP_ROOT/installed-mock.json"
                "$TEMP_ROOT/install-dms.log"
                "$TEMP_ROOT/config/DankMaterialShell/plugins/dankLookup/plugin.json"
            )
            ;;
        middle)
            injection_target="$TEMP_ROOT/installed-cli.json"
            later_paths=(
                "$TEMP_ROOT/installed-mock.json"
                "$TEMP_ROOT/install-dms.log"
                "$TEMP_ROOT/config/DankMaterialShell/plugins/dankLookup/plugin.json"
            )
            ;;
        late)
            injection_target="$TEMP_ROOT/installed-mock.json"
            later_paths=(
                "$TEMP_ROOT/install-dms.log"
                "$TEMP_ROOT/config/DankMaterialShell/plugins/dankLookup/plugin.json"
            )
            ;;
        *)
            printf 'unknown injection case: %s\n' "$injection_case" >&2
            return 1
            ;;
    esac

    local output
    output="$(run_check install_parity check_install_parity 2>&1)"
    if [[ "$output" != *"INJECTED_INSTALL_PARITY_FAILURE case=$injection_case"* ]]; then
        printf 'injected failure was not exercised: %s\n' "$injection_case" >&2
        printf '%s\n' "$output" >&2
        return 1
    fi
    if [[ "$output" == *"PASS  install_parity"* ]]; then
        printf 'run_check printed PASS after injected failure: %s\n' \
            "$injection_case" >&2
        printf '%s\n' "$output" >&2
        return 1
    fi
    if [[ "$output" != *"FAIL  install_parity"* ]]; then
        printf 'run_check did not report injected failure: %s\n' \
            "$injection_case" >&2
        printf '%s\n' "$output" >&2
        return 1
    fi

    local path
    for path in "${later_paths[@]}"; do
        assert_absent "$path"
    done
    printf '  PASS  install_parity_fail_fast_%s\n' "$injection_case"
}

for injection_case in early middle late; do
    run_injection_case "$injection_case"
done
