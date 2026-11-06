#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
cd "$REPO_ROOT"

failures=0
TEMP_ROOT="$(mktemp -d)"
trap 'rm -rf -- "$TEMP_ROOT"' EXIT

pass() {
    printf '  PASS  %s\n' "$1"
}

fail() {
    printf '  FAIL  %s\n' "$1"
    failures=$((failures + 1))
}

run_check() {
    local label="$1"
    shift
    if "$@"; then
        pass "$label"
    else
        fail "$label"
    fi
}

check_no_absolute_paths() {
    ! rg -n '/home/[^/]+/' bin lib qml standalone plugin language-packs systemd packaging \
        --glob '!pre-release-check.sh' >/dev/null
}

check_no_dictionary_data() {
    ! find . -type f \
        \( -name '*.ifo' -o -name '*.idx' -o -name '*.idx.gz' -o \
           -name '*.dict' -o -name '*.dict.gz' -o -name '*.dict.dz' -o \
           -name '*.syn' \) \
        -not -path './.git/*' -print -quit | grep -q .
}

check_no_runtime_output() {
    ! find . -type f \
        \( -name 'latest.json' -o -name 'backend_result.json' -o -name '*.socket' \) \
        -not -path './.git/*' -print -quit | grep -q .
}

check_shell_syntax() {
    local file
    while IFS= read -r -d '' file; do
        if head -n 1 "$file" | grep -Eq '^#!.*(ba)?sh([[:space:]]|$)'; then
            bash -n "$file" || return 1
        fi
    done < <(find bin lib packaging language-packs -type f -print0)
}

check_shellcheck() {
    local files=()
    local file
    if ! command -v shellcheck >/dev/null 2>&1; then
        return 77
    fi
    while IFS= read -r -d '' file; do
        if head -n 1 "$file" | grep -Eq '^#!.*(ba)?sh([[:space:]]|$)'; then
            files+=("$file")
        fi
    done < <(find bin lib packaging language-packs -type f -print0)
    shellcheck "${files[@]}"
}

check_python_test_dependencies() {
    python3 packaging/check-python-dependencies.py \
        requirements-dev.txt tests/fixtures-sdcv-stdin/evidence-schema.json
}

check_python_syntax() {
    local files=()
    local file
    while IFS= read -r -d '' file; do
        if [[ "$file" == *.py ]] || head -n 1 "$file" | grep -q '^#!/usr/bin/env python3'; then
            files+=("$file")
        fi
    done < <(find bin lib language-packs packaging tests -type f -print0)
    PYTHONPYCACHEPREFIX="$TEMP_ROOT/pycache" python3 -m py_compile "${files[@]}"
}

check_standalone_independence() {
    ! rg -n '\bqs\.|DankMaterialShell|dev\.noctalia\.noctalia-qs' standalone qml >/dev/null
}

check_resident_clipboard_boundary() {
    ! rg -n 'wl-paste|\bClipboard\b' standalone qml systemd >/dev/null
}

check_manifests_and_config() {
    python3 - language-packs/manifest.json language-packs/default-config.json plugin/plugin.json plugin/DankLookup.qml <<'PY'
import json
import pathlib
import re
import sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
config = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
plugin = json.loads(pathlib.Path(sys.argv[3]).read_text(encoding="utf-8"))
dms_wrapper = pathlib.Path(sys.argv[4]).read_text(encoding="utf-8")

expected_modes = ["han", "en", "ja", "yue"]
assert manifest["schema_version"] == 2
assert list(manifest["modes"]) == expected_modes
assert list(config["modes"]) == expected_modes
assert manifest["policy"]["dictionary_payload_included"] is False
assert manifest["policy"]["automatic_downloads"] is False
assert manifest["policy"]["network_install_supported"] is False
assert manifest["policy"]["definition_format"]["source_override_values"] == ["html", "plain"]

entries = manifest["dictionaries"]
ids = {item["id"] for item in entries}
assert len(ids) == len(entries)
assert all(item["payload_included"] is False for item in entries)
assert all(item["redistribution"] in {"unverified", "do_not_redistribute", "redistributable"} for item in entries)
assert next(item for item in entries if item["id"] == "words-hk")["redistribution"] == "do_not_redistribute"
for dictionary_id in ("moedict-revised", "xdict-han-en", "xdict-en-han", "reader-dict-ja"):
    assert next(item for item in entries if item["id"] == dictionary_id)["redistribution"] == "unverified"

by_id = {item["id"]: item for item in entries}
for mode in expected_modes:
    declared_ids = manifest["modes"][mode]["dictionary_ids"]
    assert declared_ids
    assert all(dictionary_id in ids for dictionary_id in declared_ids)
    manifest_names = {by_id[dictionary_id]["dictionary_name"] for dictionary_id in declared_ids}
    config_names = {source["name"] for source in config["modes"][mode]["sources"]}
    assert manifest_names == config_names, (mode, manifest_names, config_names)

assert plugin["id"] == "dankLookup"
assert plugin["type"] == "daemon"
assert plugin["component"] == "./DankLookup.qml"
assert plugin["requires_dms"] == ">=1.4.6"
assert "process" in plugin["permissions"]
required_plugin_fields = {
    "id", "name", "description", "version", "author", "type",
    "capabilities", "component",
}
assert required_plugin_fields <= plugin.keys()
assert re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", plugin["id"])
assert re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", plugin["version"])
assert re.fullmatch(r"(?:>=?|<=?|=|>|<)\d+\.\d+\.\d+", plugin["requires_dms"])
assert plugin["type"] in {"widget", "daemon", "launcher", "desktop"}
assert plugin["capabilities"] and all(isinstance(item, str) for item in plugin["capabilities"])
assert re.fullmatch(r"\./.*\.qml", plugin["component"])
assert set(plugin.get("permissions", [])) <= {"settings_read", "settings_write", "process", "network"}
if plugin["type"] == "launcher":
    assert isinstance(plugin.get("trigger"), str)
for mapping in (
    "primary: Theme.primary",
    "on_primary: Theme.onPrimary",
    "surface: Theme.hostSurface",
    "surface_container_high: Theme.surfaceContainerHigh",
    "on_surface: Theme.surfaceText",
    "outline: Theme.outline\n",
    "error: Theme.error",
    "on_error: Theme.onError",
    "dark: !Theme.isLightMode",
    "cornerRadius: Theme.cornerRadius",
    "surfaceOpacity: Theme.popupTransparency",
    "Shared.LookupApplication",
    "themeHost: dmsTheme",
):
    assert mapping in dms_wrapper, mapping
# outlineMedium is scaled by blurLayerOutlineOpacity (0 by default): invisible.
assert ": Theme.outlineMedium" not in dms_wrapper
PY
}

check_language_pack_status() {
    local python_bin
    python_bin="$(command -v python3)"
    python3 language-packs/dank-lookup-language-packs \
        --resource-dir language-packs status --json >"$TEMP_ROOT/language-status.json"
    python3 - "$TEMP_ROOT/language-status.json" <<'PY'
import json
import pathlib
import sys

status = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert status["schema_version"] == 1
assert list(status["modes"]) == ["han", "en", "ja", "yue"]
assert all("available" in mode for mode in status["modes"].values())
assert all(item["payload_included"] is False for item in status["dictionaries"])
PY

    mkdir -p "$TEMP_ROOT/empty-path"
    PATH="$TEMP_ROOT/empty-path" "$python_bin" language-packs/dank-lookup-language-packs \
        --resource-dir language-packs status --json >"$TEMP_ROOT/language-status-missing.json"
    python3 - "$TEMP_ROOT/language-status-missing.json" <<'PY'
import json
import pathlib
import sys

status = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert status["sdcv_error"] == "sdcv is not installed"
assert all(mode["available"] is False for mode in status["modes"].values())
assert all(item["detected"] is False for item in status["dictionaries"])
PY
}

check_install_parity() {
    local fake_home="$TEMP_ROOT/home"
    local fake_config="$TEMP_ROOT/config"
    local fake_data="$TEMP_ROOT/data"
    local fake_runtime="$TEMP_ROOT/runtime"
    mkdir -p "$fake_home" "$fake_config" "$fake_data" "$fake_runtime" || return 1
    chmod 700 "$fake_runtime" || return 1

    HOME="$fake_home" XDG_CONFIG_HOME="$fake_config" XDG_DATA_HOME="$fake_data" \
        packaging/install-user.sh --without-tokenizer >"$TEMP_ROOT/install-fresh.log" || return 1
    python3 - "$fake_config/dank-lookup/config.json" <<'PY' || return 1
import json
import pathlib
import sys

config = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert list(config["modes"]) == ["han", "en", "ja", "yue"]
PY
    [[ "$(stat -c '%a' "$fake_config/dank-lookup/config.json")" == 600 ]] || return 1

    install -Dm600 tests/configs/ci-minimal.json \
        "$fake_config/dank-lookup/config.json" || return 1
    local before
    before="$(sha256sum "$fake_config/dank-lookup/config.json" | cut -d' ' -f1)" \
        || return 1

    HOME="$fake_home" XDG_CONFIG_HOME="$fake_config" XDG_DATA_HOME="$fake_data" \
        packaging/install-user.sh --without-tokenizer >"$TEMP_ROOT/install-repeat.log" || return 1

    local after
    after="$(sha256sum "$fake_config/dank-lookup/config.json" | cut -d' ' -f1)" \
        || return 1
    [[ "$before" == "$after" ]] || return 1

    local required=(
        "$fake_home/.local/bin/dank-lookup"
        "$fake_home/.local/bin/dank-lookup-selection"
        "$fake_home/.local/bin/dank-lookup-language-packs"
        "$fake_home/.local/lib/dank-lookup/dank_lookup_core.py"
        "$fake_home/.local/lib/dank-lookup/dank_lookup_runtime.py"
        "$fake_home/.local/lib/dank-lookup/dank_lookup_sdcv_stdin.py"
        "$fake_home/.local/lib/dank-lookup/dank_lookup_detectors.py"
        "$fake_home/.local/lib/dank-lookup/backends/sdcv.py"
        "$fake_home/.local/lib/dank-lookup/backends/mock.py"
        "$fake_home/.local/lib/dank-lookup/language-packs/manifest.json"
        "$fake_config/quickshell/dank-lookup/shell.qml"
        "$fake_config/systemd/user/dank-lookup.service"
        "$fake_data/dank-lookup/matugen/dark-template.json"
        "$fake_data/dank-lookup/matugen/light-template.json"
    )
    local path
    for path in "${required[@]}"; do
        [[ -f "$path" ]] || return 1
    done
    [[ ! -e "$fake_config/DankMaterialShell/plugins/dankLookup/plugin.json" ]] || return 1

    printf '%s' 'fresh install' \
    | HOME="$fake_home" XDG_CONFIG_HOME="$fake_config" XDG_DATA_HOME="$fake_data" \
        XDG_RUNTIME_DIR="$fake_runtime" \
        DANK_LOOKUP_BACKEND_IDLE_TIMEOUT=0.1 \
        DANK_LOOKUP_SDCV="$TEMP_ROOT/no-such-sdcv" \
        "$fake_home/.local/bin/dank-lookup" lookup \
        >"$TEMP_ROOT/installed-cli.json" || return 1
    python3 - "$TEMP_ROOT/installed-cli.json" \
        "$fake_config/dank-lookup/config.json" <<'PY' || return 1
import json
import pathlib
import sys

result = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert result["backend"] == "unavailable"
assert result["error"]["code"] == "sdcv_missing"
assert result["available_modes"] == []
assert result["config"]["loaded"] is True
assert pathlib.Path(result["config"]["path"]) == pathlib.Path(sys.argv[2])
PY

    printf '%s' 'stdin-safe query' \
    | "$fake_home/.local/lib/dank-lookup/backends/mock.sh" \
        --stdout --stdin --request-id installed-mock --sequence 17 \
        >"$TEMP_ROOT/installed-mock.json" || return 1
    python3 - "$TEMP_ROOT/installed-mock.json" <<'PY' || return 1
import json
import pathlib
import sys

result = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert result["backend"] == "mock"
assert result["query"] == "stdin-safe query"
assert result["request_id"] == "installed-mock"
assert result["sequence"] == 17
assert result["runtime"]["retained"] is False
PY

    HOME="$fake_home" XDG_CONFIG_HOME="$fake_config" XDG_DATA_HOME="$fake_data" \
        packaging/install-user.sh --with-dms --without-tokenizer \
        >"$TEMP_ROOT/install-dms.log" || return 1
    [[ -f "$fake_config/DankMaterialShell/plugins/dankLookup/plugin.json" ]] \
        || return 1
    [[ -f "$fake_config/DankMaterialShell/plugins/dankLookup/shared/LookupApplication.qml" ]] \
        || return 1
}

main() {
    echo '== Dank Lookup pre-release check =='

    echo '--- source safety ---'
    run_check absolute_paths check_no_absolute_paths
    run_check dictionary_payload check_no_dictionary_data
    run_check runtime_output check_no_runtime_output

    echo '--- syntax and standalone boundary ---'
    run_check shell_syntax check_shell_syntax
    if check_shellcheck; then
        pass shellcheck
    elif [[ $? -eq 77 ]]; then
        printf '  FAIL  shellcheck (not installed)\n'
        failures=$((failures + 1))
    else
        fail shellcheck
    fi
    run_check python_test_dependencies check_python_test_dependencies
    run_check dependency_preflight_tests \
        python3 -m unittest -v tests.test_dependency_preflight
    run_check python_syntax check_python_syntax
    run_check standalone_no_dms check_standalone_independence
    run_check resident_no_clipboard_read check_resident_clipboard_boundary

    echo '--- manifests and delivery ---'
    run_check manifest_schema check_manifests_and_config
    run_check language_pack_status check_language_pack_status
    run_check install_parity_fail_fast bash tests/test_pre_release_fail_fast.sh
    run_check install_parity check_install_parity
    run_check managed_install_matrix python3 tests/test_packaging.py
    run_check required_files test -f LICENSE -a -f CHANGELOG.md -a -f README.md \
        -a -f packaging/managed-files.py -a -f packaging/legacy-managed-files.json \
        -a -f packaging/build-review-bundle.sh \
        -a -f packaging/release_evidence.py \
        -a -f packaging/review-evidence-requirements.json \
        -a -f packaging/check-python-dependencies.py \
        -a -f requirements-dev.txt \
        -a -f tests/test_dependency_preflight.py \
        -a -f tests/test_pre_release_fail_fast.sh

    echo
    if ((failures == 0)); then
        echo 'All checks passed.'
    else
        printf '%d check(s) failed.\n' "$failures"
        return 1
    fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
