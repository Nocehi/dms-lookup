#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
MANIFEST_TOOL="$SCRIPT_DIR/managed-files.py"
LEGACY_MANIFEST="$SCRIPT_DIR/legacy-managed-files.json"
PURGE_CONFIG=false

usage() {
    cat <<'EOF'
Usage: packaging/uninstall-user.sh [--purge-config]

Remove only validated managed-manifest entries (or explicitly listed legacy
entries for marker-based old installs). StarDict data is never removed.

  --purge-config  also remove ~/.config/dank-lookup/config.json
  -h, --help      show this help
EOF
}

while (($#)); do
    case "$1" in
        --purge-config) PURGE_CONFIG=true ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'uninstall-user.sh: unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
DEST_BIN="$HOME/.local/bin"
DEST_LIB="$HOME/.local/lib/dank-lookup"
DEST_QS="$XDG_CONFIG_HOME/quickshell/dank-lookup"
DEST_SYSTEMD="$XDG_CONFIG_HOME/systemd/user"
DEST_DMS="$XDG_CONFIG_HOME/DankMaterialShell/plugins/dankLookup"
DEST_MATUGEN="$XDG_DATA_HOME/dank-lookup/matugen"
MANAGED_MANIFEST="$DEST_LIB/managed-files.json"

TEMP_ROOT="$(mktemp -d)"
trap 'rm -rf -- "$TEMP_ROOT"' EXIT
RECORDS="$TEMP_ROOT/managed.tsv"
: >"$RECORDS"

root_path() {
    case "$1" in
        home_bin) printf '%s\n' "$DEST_BIN" ;;
        app_lib) printf '%s\n' "$DEST_LIB" ;;
        quickshell) printf '%s\n' "$DEST_QS" ;;
        systemd_user) printf '%s\n' "$DEST_SYSTEMD" ;;
        matugen) printf '%s\n' "$DEST_MATUGEN" ;;
        dms) printf '%s\n' "$DEST_DMS" ;;
        *) return 1 ;;
    esac
}

scope_was_legacy_managed() {
    case "$1" in
        home_bin|app_lib|systemd_user) [[ -f "$DEST_LIB/.installed-by-dank-lookup" ]] ;;
        quickshell) [[ -f "$DEST_QS/.installed-by-dank-lookup" ]] ;;
        matugen) [[ -f "$DEST_MATUGEN/.installed-by-dank-lookup" ]] ;;
        dms) [[ -f "$DEST_DMS/.installed-by-dank-lookup" ]] ;;
        *) return 1 ;;
    esac
}

if [[ -e "$MANAGED_MANIFEST" ]]; then
    if [[ ! -f "$MANAGED_MANIFEST" || -L "$MANAGED_MANIFEST" ]] ||
       ! python3 "$MANIFEST_TOOL" validate "$MANAGED_MANIFEST"; then
        printf 'uninstall-user.sh: refusing invalid managed manifest: %s\n' \
            "$MANAGED_MANIFEST" >&2
        exit 1
    fi
    python3 "$MANIFEST_TOOL" paths "$MANAGED_MANIFEST" >"$RECORDS"
else
    while IFS=$'\t' read -r root_key relative; do
        scope_was_legacy_managed "$root_key" || continue
        root="$(root_path "$root_key")"
        destination="$root/$relative"
        if [[ -e "$destination" || -L "$destination" ]]; then
            printf '%s\t%s\n' "$root_key" "$relative" >>"$RECORDS"
        fi
    done < <(python3 "$MANIFEST_TOOL" paths "$LEGACY_MANIFEST")
fi

prune_parent_directories() {
    local path="$1"
    local root="$2"
    local directory
    directory="$(dirname -- "$path")"
    while [[ "$directory" != "$root" && "$directory" == "$root"/* ]]; do
        rmdir -- "$directory" 2>/dev/null || break
        directory="$(dirname -- "$directory")"
    done
}

echo 'Removing validated Dank Lookup managed files...'
while IFS=$'\t' read -r root_key relative; do
    [[ -n "$root_key" ]] || continue
    root="$(root_path "$root_key")"
    destination="$root/$relative"
    if [[ -f "$destination" || -L "$destination" ]]; then
        rm -f -- "$destination"
        prune_parent_directories "$destination" "$root"
    fi
done <"$RECORDS"

for root_key in app_lib quickshell matugen dms; do
    root="$(root_path "$root_key")"
    rmdir -- "$root" 2>/dev/null || true
done
rmdir -- "$XDG_DATA_HOME/dank-lookup" 2>/dev/null || true

if "$PURGE_CONFIG"; then
    rm -f -- "$XDG_CONFIG_HOME/dank-lookup/config.json"
    rmdir -- "$XDG_CONFIG_HOME/dank-lookup" 2>/dev/null || true
    echo '  Removed routing config because --purge-config was explicit.'
else
    echo "  Preserved routing config: $XDG_CONFIG_HOME/dank-lookup/config.json"
fi

echo '  Preserved all StarDict dictionary data and every unlisted file.'
echo 'No running process or service state was changed.'
