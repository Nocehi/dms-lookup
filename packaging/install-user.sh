#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
MANIFEST_TOOL="$SCRIPT_DIR/managed-files.py"
LEGACY_MANIFEST="$SCRIPT_DIR/legacy-managed-files.json"
WITH_DMS=false
WITH_TOKENIZER=false
WITHOUT_TOKENIZER=false
TOKENIZER_SOURCE=""

usage() {
    cat <<'EOF'
Usage: packaging/install-user.sh [--with-dms]
                                 [--with-tokenizer | --tokenizer-bin PATH | --without-tokenizer]

Install standalone Dank Lookup and record every installed file in a managed
manifest. Existing ~/.config/dank-lookup/config.json is never overwritten.

  --with-dms            install or upgrade the optional DMS compatibility wrapper
  --with-tokenizer      build tokenizer/ with `cargo build --release --locked`
                        and install it as a managed file
  --tokenizer-bin PATH  install an already built tokenizer binary instead
  --without-tokenizer   install without a tokenizer; segmentation then uses the
                        non-linguistic bigram fallback
  -h, --help            show this help

Omitting --with-dms preserves an already managed DMS scope; omitting the
tokenizer options keeps an installed tokenizer that speaks the stdin CLI. If
none is installed, or the installed one predates the stdin CLI (the v0.7.1
build), the installer stops before changing anything; it never builds one
unasked and never overwrites an unmanaged tokenizer. The installer does
not download dictionaries, start/reload Quickshell, edit compositor keybinds,
or reload DMS.
EOF
}

while (($#)); do
    case "$1" in
        --with-dms) WITH_DMS=true ;;
        --with-tokenizer) WITH_TOKENIZER=true ;;
        --without-tokenizer) WITHOUT_TOKENIZER=true ;;
        --tokenizer-bin)
            if (($# < 2)); then
                printf 'install-user.sh: --tokenizer-bin needs a path\n' >&2
                exit 2
            fi
            TOKENIZER_SOURCE="$2"
            shift
            ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'install-user.sh: unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
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
RECORDS="$TEMP_ROOT/records.tsv"
OLD_MANIFEST="$TEMP_ROOT/old-managed-files.json"
NEW_MANIFEST="$TEMP_ROOT/new-managed-files.json"
: >"$RECORDS"
HAVE_OLD_MANIFEST=false
DMS_PRESERVED=false

required_sources=(
    bin/dank-lookup
    bin/dank-lookup-selection
    lib/dank_lookup_core.py
    lib/dank_lookup_runtime.py
    lib/dank_lookup_sdcv_stdin.py
    lib/dank_lookup_detectors.py
    lib/backends/sdcv.sh
    lib/backends/sdcv.py
    lib/backends/mock.sh
    lib/backends/mock.py
    standalone/shell.qml
    language-packs/dank-lookup-language-packs
    language-packs/manifest.json
    language-packs/default-config.json
    qml/matugen-dark-template.json
    qml/matugen-light-template.json
    systemd/dank-lookup.service
    packaging/managed-files.py
    packaging/legacy-managed-files.json
)
if "$WITH_DMS"; then
    required_sources+=(plugin/plugin.json plugin/DankLookup.qml)
fi
for relative in "${required_sources[@]}"; do
    if [[ ! -f "$REPO_ROOT/$relative" ]]; then
        printf 'install-user.sh: required source is missing: %s\n' "$relative" >&2
        exit 1
    fi
done
if [[ ! -d "$REPO_ROOT/qml" ]] ||
   ! find "$REPO_ROOT/qml" -type f -name '*.qml' -print -quit | grep -q .; then
    printf 'install-user.sh: no shared QML files found under qml/\n' >&2
    exit 1
fi

tokenizer_options=0
"$WITH_TOKENIZER" && tokenizer_options=$((tokenizer_options + 1))
[[ -n "$TOKENIZER_SOURCE" ]] && tokenizer_options=$((tokenizer_options + 1))
"$WITHOUT_TOKENIZER" && tokenizer_options=$((tokenizer_options + 1))
if ((tokenizer_options > 1)); then
    printf 'install-user.sh: use only one of --with-tokenizer, --tokenizer-bin, --without-tokenizer\n' >&2
    exit 2
fi

# The tokenizer reads the query from stdin. Its v0.7.1 predecessor took the
# query as positional text, answers --stdin with "unknown option" and leaves
# segmentation on the non-linguistic bigram fallback.
tokenizer_speaks_stdin() {
    local output
    output="$(printf '測試\n' | timeout 10 "$1" --lang zh --json --stdin 2>/dev/null)" || return 1
    [[ "$output" == *'"tokens"'* ]]
}

if [[ -n "$TOKENIZER_SOURCE" ]] && ! tokenizer_speaks_stdin "$TOKENIZER_SOURCE"; then
    printf 'install-user.sh: tokenizer does not answer the stdin CLI: %s\n' \
        "$TOKENIZER_SOURCE" >&2
    exit 1
fi

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

target_path() {
    local root
    root="$(root_path "$1")" || return 1
    printf '%s/%s\n' "$root" "$2"
}

record_managed() {
    local root_key="$1"
    local relative="$2"
    local destination="$3"
    if [[ ! -f "$destination" || -L "$destination" ]]; then
        printf 'install-user.sh: installed file is not a regular file: %s\n' "$destination" >&2
        exit 1
    fi
    printf '%s\t%s\n' "$root_key" "$relative" >>"$RECORDS"
}

install_managed() {
    local mode="$1"
    local source="$2"
    local root_key="$3"
    local relative="$4"
    local destination
    destination="$(target_path "$root_key" "$relative")"
    install -Dm"$mode" "$source" "$destination"
    record_managed "$root_key" "$relative" "$destination"
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
        printf 'install-user.sh: refusing invalid managed manifest: %s\n' \
            "$MANAGED_MANIFEST" >&2
        exit 1
    fi
    cp -- "$MANAGED_MANIFEST" "$OLD_MANIFEST"
    HAVE_OLD_MANIFEST=true
else
    legacy_records="$TEMP_ROOT/legacy-records.tsv"
    : >"$legacy_records"
    while IFS=$'\t' read -r root_key relative; do
        if ! scope_was_legacy_managed "$root_key"; then
            continue
        fi
        destination="$(target_path "$root_key" "$relative")"
        if [[ -f "$destination" && ! -L "$destination" ]]; then
            printf '%s\t%s\n' "$root_key" "$relative" >>"$legacy_records"
        fi
    done < <(python3 "$MANIFEST_TOOL" paths "$LEGACY_MANIFEST")
    if [[ -s "$legacy_records" ]]; then
        python3 "$MANIFEST_TOOL" build \
            --records "$legacy_records" --output "$OLD_MANIFEST"
        HAVE_OLD_MANIFEST=true
    fi
fi

check_unmanaged_directory() {
    local directory="$1"
    local marker="$2"
    local label="$3"
    if [[ -d "$directory" && ! -f "$directory/$marker" ]] &&
       find "$directory" -mindepth 1 -print -quit | grep -q .; then
        printf 'install-user.sh: refusing to overwrite unmanaged %s: %s\n' \
            "$label" "$directory" >&2
        exit 1
    fi
}
check_unmanaged_directory "$DEST_QS" .installed-by-dank-lookup 'Quickshell config'
if "$WITH_DMS"; then
    check_unmanaged_directory "$DEST_DMS" .installed-by-dank-lookup 'DMS plugin'
fi
if [[ -e "$DEST_SYSTEMD/dank-lookup.service" &&
      ! -f "$DEST_LIB/.installed-by-dank-lookup" &&
      ! -f "$MANAGED_MANIFEST" ]] &&
   ! cmp -s "$REPO_ROOT/systemd/dank-lookup.service" \
        "$DEST_SYSTEMD/dank-lookup.service"; then
    printf 'install-user.sh: refusing to overwrite unmanaged user service: %s\n' \
        "$DEST_SYSTEMD/dank-lookup.service" >&2
    exit 1
fi

TOKENIZER_DEST="$DEST_LIB/dank-lookup-tokenize"
tokenizer_was_managed() {
    "$HAVE_OLD_MANIFEST" &&
        python3 "$MANIFEST_TOOL" paths "$OLD_MANIFEST" |
        grep -qxF "$(printf 'app_lib\tdank-lookup-tokenize')"
}
tokenizer_fail() {
    {
        echo
        echo '!!! install-user.sh: tokenizer problem, nothing was installed !!!'
        printf '%s\n' "$@"
        echo 'Rerun with one of:'
        echo '  --with-tokenizer       build tokenizer/ with cargo (stdin CLI)'
        echo '  --tokenizer-bin PATH   install an already built stdin-CLI tokenizer'
        echo '  --without-tokenizer    use the non-linguistic bigram fallback'
    } >&2
    exit 1
}
# Decide the tokenizer before the first file is installed, so a missing or
# pre-stdin tokenizer stops the install instead of degrading segmentation.
if [[ -e "$TOKENIZER_DEST" || -L "$TOKENIZER_DEST" ]]; then
    if { "$WITH_TOKENIZER" || [[ -n "$TOKENIZER_SOURCE" ]]; } && ! tokenizer_was_managed; then
        tokenizer_fail \
            "refusing to overwrite unmanaged tokenizer: $TOKENIZER_DEST" \
            'Move it aside first (a v0.7.1 install still needs it), then rerun.'
    fi
    if ! "$WITH_TOKENIZER" && [[ -z "$TOKENIZER_SOURCE" ]] &&
       ! tokenizer_speaks_stdin "$TOKENIZER_DEST"; then
        tokenizer_fail \
            "incompatible tokenizer: $TOKENIZER_DEST does not answer the stdin CLI" \
            '(probably the v0.7.1 positional-text build). Lookups would report' \
            'tokenizer_incompatible and segment with the bigram fallback.' \
            'Move it aside (a v0.7.1 install still needs it), then rerun.'
    fi
elif ! "$WITH_TOKENIZER" && [[ -z "$TOKENIZER_SOURCE" ]] && ! "$WITHOUT_TOKENIZER"; then
    tokenizer_fail "no tokenizer installed at $TOKENIZER_DEST"
fi
if "$WITH_TOKENIZER"; then
    if ! command -v cargo >/dev/null 2>&1; then
        printf 'install-user.sh: --with-tokenizer needs cargo on PATH\n' >&2
        exit 1
    fi
    # Note: lindera-ipadic's build script downloads IPADIC even when cargo
    # itself has every crate cached.
    cargo build --release --locked --quiet \
        --manifest-path "$REPO_ROOT/tokenizer/Cargo.toml" \
        --target-dir "$TEMP_ROOT/tokenizer-target"
    TOKENIZER_SOURCE="$TEMP_ROOT/tokenizer-target/release/dank-lookup-tokenize"
    if ! tokenizer_speaks_stdin "$TOKENIZER_SOURCE"; then
        printf 'install-user.sh: built tokenizer does not answer the stdin CLI: %s\n' \
            "$TOKENIZER_SOURCE" >&2
        exit 1
    fi
fi

echo 'Installing standalone Dank Lookup to user-scope paths...'

install_managed 755 "$REPO_ROOT/bin/dank-lookup" home_bin dank-lookup
install_managed 755 "$REPO_ROOT/bin/dank-lookup-selection" home_bin dank-lookup-selection
install_managed 755 "$REPO_ROOT/language-packs/dank-lookup-language-packs" \
    home_bin dank-lookup-language-packs

install_managed 644 "$REPO_ROOT/lib/dank_lookup_core.py" app_lib dank_lookup_core.py
install_managed 644 "$REPO_ROOT/lib/dank_lookup_runtime.py" app_lib dank_lookup_runtime.py
install_managed 644 "$REPO_ROOT/lib/dank_lookup_sdcv_stdin.py" \
    app_lib dank_lookup_sdcv_stdin.py
install_managed 644 "$REPO_ROOT/lib/dank_lookup_detectors.py" \
    app_lib dank_lookup_detectors.py
install_managed 755 "$REPO_ROOT/lib/backends/sdcv.sh" app_lib backends/sdcv.sh
install_managed 644 "$REPO_ROOT/lib/backends/sdcv.py" app_lib backends/sdcv.py
install_managed 755 "$REPO_ROOT/lib/backends/mock.sh" app_lib backends/mock.sh
install_managed 644 "$REPO_ROOT/lib/backends/mock.py" app_lib backends/mock.py
install_managed 755 "$REPO_ROOT/language-packs/dank-lookup-language-packs" \
    app_lib language-packs/dank-lookup-language-packs
install_managed 644 "$REPO_ROOT/language-packs/manifest.json" \
    app_lib language-packs/manifest.json
install_managed 644 "$REPO_ROOT/language-packs/default-config.json" \
    app_lib language-packs/default-config.json

install_managed 644 "$REPO_ROOT/standalone/shell.qml" quickshell shell.qml
while IFS= read -r -d '' source; do
    relative="${source#"$REPO_ROOT/qml/"}"
    install_managed 644 "$source" quickshell "qml/$relative"
done < <(find "$REPO_ROOT/qml" -type f -name '*.qml' -print0 | sort -z)
install_managed 600 /dev/null quickshell .installed-by-dank-lookup

install_managed 644 "$REPO_ROOT/qml/matugen-dark-template.json" \
    matugen dark-template.json
install_managed 644 "$REPO_ROOT/qml/matugen-light-template.json" \
    matugen light-template.json
install_managed 600 /dev/null matugen .installed-by-dank-lookup
install_managed 644 "$REPO_ROOT/systemd/dank-lookup.service" \
    systemd_user dank-lookup.service
if [[ -n "$TOKENIZER_SOURCE" ]]; then
    install_managed 755 "$TOKENIZER_SOURCE" app_lib dank-lookup-tokenize
elif tokenizer_was_managed && [[ -f "$TOKENIZER_DEST" ]]; then
    record_managed app_lib dank-lookup-tokenize "$TOKENIZER_DEST"
fi
install_managed 600 /dev/null app_lib .installed-by-dank-lookup

# Name the commit this copy came from, so owner-run checks of the installed
# card can be tied to it (tests/source_identity.py). Untracked files count as
# changes because the qml/ glob installs untracked QML files as well.
INSTALLED_SOURCE="$TEMP_ROOT/installed-source.json"
if source_commit="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null)" \
    && source_status="$(git -C "$REPO_ROOT" status --porcelain -- \
        bin lib qml standalone plugin language-packs systemd 2>/dev/null)"; then
    if [[ -n "$source_status" ]]; then source_changes=true; else source_changes=false; fi
    printf '{"commit": "%s", "uncommitted_changes": %s}\n' \
        "$source_commit" "$source_changes" >"$INSTALLED_SOURCE"
else
    printf '{"commit": null, "uncommitted_changes": true}\n' >"$INSTALLED_SOURCE"
fi
install_managed 644 "$INSTALLED_SOURCE" app_lib installed-source.json

"$DEST_BIN/dank-lookup-language-packs" install-config

if "$WITH_DMS"; then
    install_managed 644 "$REPO_ROOT/plugin/plugin.json" dms plugin.json
    install_managed 644 "$REPO_ROOT/plugin/DankLookup.qml" dms DankLookup.qml
    while IFS= read -r -d '' source; do
        relative="${source#"$REPO_ROOT/qml/"}"
        install_managed 644 "$source" dms "shared/$relative"
    done < <(find "$REPO_ROOT/qml" -type f -name '*.qml' -print0 | sort -z)
    install_managed 600 /dev/null dms .installed-by-dank-lookup
elif "$HAVE_OLD_MANIFEST"; then
    while IFS=$'\t' read -r root_key relative; do
        [[ "$root_key" == dms ]] || continue
        destination="$(target_path "$root_key" "$relative")"
        if [[ -e "$destination" && ( ! -f "$destination" || -L "$destination" ) ]]; then
            printf 'install-user.sh: refusing unsafe preserved DMS entry: %s\n' \
                "$destination" >&2
            exit 1
        fi
        if [[ -f "$destination" ]]; then
            record_managed "$root_key" "$relative" "$destination"
            DMS_PRESERVED=true
        fi
    done < <(python3 "$MANIFEST_TOOL" paths "$OLD_MANIFEST")
fi

# The manifest describes itself. Every other record has already been verified
# against an installed regular file before this final build.
printf '%s\t%s\n' app_lib managed-files.json >>"$RECORDS"
python3 "$MANIFEST_TOOL" build --records "$RECORDS" --output "$NEW_MANIFEST"

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

if "$HAVE_OLD_MANIFEST"; then
    python3 "$MANIFEST_TOOL" stale --old "$OLD_MANIFEST" --new "$NEW_MANIFEST" \
        >"$TEMP_ROOT/stale.tsv"
    while IFS=$'\t' read -r root_key relative; do
        [[ -n "$root_key" ]] || continue
        root="$(root_path "$root_key")"
        destination="$root/$relative"
        if [[ -f "$destination" || -L "$destination" ]]; then
            rm -f -- "$destination"
            prune_parent_directories "$destination" "$root"
        fi
    done <"$TEMP_ROOT/stale.tsv"
fi

install -Dm600 "$NEW_MANIFEST" "$MANAGED_MANIFEST"

echo
echo 'Standalone files installed; no process was started or reloaded.'
echo 'Inspect dictionary availability: dank-lookup-language-packs status'
echo 'Start/reload any user service or DMS plugin only as an explicit later action.'
if "$WITH_DMS"; then
    echo 'Optional DMS wrapper installed; it was not reloaded.'
elif "$DMS_PRESERVED"; then
    echo 'Existing managed DMS wrapper scope preserved unchanged.'
else
    echo 'No DMS wrapper scope was installed.'
fi
if [[ ! -e "$TOKENIZER_DEST" ]]; then
    echo 'No tokenizer installed (--without-tokenizer); segmentation uses the'
    echo 'non-linguistic bigram fallback. Install one with --with-tokenizer.'
fi
