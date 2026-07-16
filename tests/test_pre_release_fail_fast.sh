#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
REAL_PYTHON="$(command -v python3)"
cd "$REPO_ROOT"

# shellcheck disable=SC1091
source packaging/pre-release-check.sh

python3() {
    if [[ "${1-}" == "-" && "${2-}" == "$TEMP_ROOT/config/dank-lookup/config.json" ]]; then
        printf '%s\n' "INJECTED_INSTALL_PARITY_FAILURE exit=42 target=${2-}" >&2
        return 42
    fi
    "$REAL_PYTHON" "$@"
}

output="$(run_check install_parity check_install_parity 2>&1)"
if [[ "$output" != *"INJECTED_INSTALL_PARITY_FAILURE"* ]]; then
    printf '%s\n' "injected failure was not exercised" >&2
    printf '%s\n' "$output" >&2
    exit 1
fi
if [[ "$output" == *"PASS  install_parity"* ]]; then
    printf '%s\n' "run_check printed PASS after an injected intermediate failure" >&2
    printf '%s\n' "$output" >&2
    exit 1
fi
if [[ "$output" != *"FAIL  install_parity"* ]]; then
    printf '%s\n' "run_check did not report the injected failure" >&2
    printf '%s\n' "$output" >&2
    exit 1
fi
