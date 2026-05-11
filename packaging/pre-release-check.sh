#!/usr/bin/env bash
set -euo pipefail
failures=0

check() {
    local label="$1"
    shift
    if "$@"; then
        echo "  PASS  $label"
    else
        echo "  FAIL  $label"
        failures=$((failures + 1))
    fi
}

echo "== dankLookup pre-release check =="

cd "$(dirname "$0")/.."

echo "--- no absolute paths ---"
check "absolute_paths" bash -c 'test -z "$(grep -Rl /home/nocehi bin lib plugin/*.qml plugin/*.json 2>/dev/null)"'

echo "--- no dictionary data ---"
check "dict_data" test -z "$(find . \( -name '*.ifo' -o -name '*.idx' -o -name '*.dict' -o -name '*.dict.dz' -o -name '*.syn' \) 2>/dev/null)"

echo "--- no runtime tmp output ---"
check "runtime_tmp" test -z "$(find . -path '*/tmp/*' -o -name 'latest.json' -o -name 'backend_result.json' 2>/dev/null)"

echo "--- shell syntax ---"
check "shell_syntax" bash -c 'bash -n bin/dank-lookup-selection && bash -n lib/backends/sdcv.sh && bash -n lib/backends/mock.sh && bash -n packaging/install-user.sh && bash -n packaging/uninstall-user.sh'

echo "--- required files ---"
check "required_files" test -f LICENSE -a -f CHANGELOG.md -a -f README.md

echo
if [ "$failures" -eq 0 ]; then
    echo "All checks passed."
else
    echo "$failures check(s) failed."
    exit 1
fi
