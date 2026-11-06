#!/usr/bin/env bash
# Experimental, opt-in: install the Magic Trackpad force-press trigger.
#
# Not part of install-user.sh and not removed by uninstall-user.sh. Installs
# only into the user's home; never runs sudo. The udev rule for haptics is
# printed for the user to install by hand.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: packaging/install-force-touch.sh [--enable] [--dry-run]
       packaging/install-force-touch.sh --uninstall [--dry-run]

Installs bin/dank-force-press-watch to ~/.local/bin and
systemd/dank-force-press.service to ~/.config/systemd/user.
The service is left disabled unless --enable is given.
An existing file that differs is kept as <name>.bak-<timestamp>.

Experimental: Apple Magic Trackpad 2 / USB-C (05ac:0265, 05ac:0324) only;
haptic feedback works over USB only. See docs/force-touch.md.
EOF
}

MODE=install
ENABLE=false
DRY_RUN=false
for arg in "$@"; do
    case "$arg" in
        --enable) ENABLE=true ;;
        --uninstall) MODE=uninstall ;;
        --dry-run) DRY_RUN=true ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'unknown option: %s\n' "$arg" >&2; usage >&2; exit 2 ;;
    esac
done
if [[ "$MODE" == uninstall ]] && "$ENABLE"; then
    printf -- '--enable and --uninstall cannot be combined\n' >&2
    exit 2
fi

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
DEST_BIN="$HOME/.local/bin/dank-force-press-watch"
DEST_UNIT="$XDG_CONFIG_HOME/systemd/user/dank-force-press.service"
SRC_BIN="$REPO_ROOT/bin/dank-force-press-watch"
SRC_UNIT="$REPO_ROOT/systemd/dank-force-press.service"
UDEV_RULE="$REPO_ROOT/packaging/udev/70-dank-magic-trackpad-haptic.rules"
UDEV_DEST=/etc/udev/rules.d/70-dank-magic-trackpad-haptic.rules
STAMP="$(date +%Y%m%d-%H%M%S)"

run() {
    if "$DRY_RUN"; then
        printf '[dry-run] %s\n' "$*"
    else
        "$@"
    fi
}

backup_if_different() {
    local src="$1" dest="$2"
    if [[ -e "$dest" ]] && ! cmp -s "$src" "$dest"; then
        run cp -p -- "$dest" "$dest.bak-$STAMP"
        printf 'kept previous %s as %s.bak-%s\n' "$dest" "$dest" "$STAMP"
    fi
}

have_systemctl() {
    command -v systemctl >/dev/null 2>&1
}

if [[ "$MODE" == uninstall ]]; then
    if have_systemctl; then
        run systemctl --user disable --now dank-force-press.service || true
    fi
    for f in "$DEST_BIN" "$DEST_UNIT"; do
        if [[ -e "$f" ]]; then
            run cp -p -- "$f" "$f.bak-$STAMP"
            run rm -f -- "$f"
            printf 'removed %s (backup %s.bak-%s)\n' "$f" "$f" "$STAMP"
        fi
    done
    if have_systemctl; then
        run systemctl --user daemon-reload
    fi
    if [[ -e "$UDEV_DEST" ]]; then
        printf '\nThe haptic udev rule is still installed. To remove it:\n'
        printf '  sudo rm %s && sudo udevadm control --reload\n' "$UDEV_DEST"
    fi
    exit 0
fi

for f in "$SRC_BIN" "$SRC_UNIT" "$UDEV_RULE"; do
    [[ -f "$f" ]] || { printf 'missing source file: %s\n' "$f" >&2; exit 1; }
done
if ! python3 -c 'import evdev' 2>/dev/null; then
    printf 'python-evdev is not importable by python3; install it first (Arch: python-evdev)\n' >&2
    exit 1
fi
if ! command -v dank-lookup-selection >/dev/null 2>&1 \
        && [[ ! -x "$HOME/.local/bin/dank-lookup-selection" ]]; then
    printf 'dank-lookup-selection not found; run packaging/install-user.sh first\n' >&2
    exit 1
fi

run mkdir -p -- "$(dirname -- "$DEST_BIN")" "$(dirname -- "$DEST_UNIT")"
backup_if_different "$SRC_BIN" "$DEST_BIN"
run install -m 755 -- "$SRC_BIN" "$DEST_BIN"
backup_if_different "$SRC_UNIT" "$DEST_UNIT"
run install -m 644 -- "$SRC_UNIT" "$DEST_UNIT"
printf 'installed %s\ninstalled %s\n' "$DEST_BIN" "$DEST_UNIT"

if have_systemctl; then
    run systemctl --user daemon-reload
    if "$ENABLE"; then
        run systemctl --user enable dank-force-press.service
        run systemctl --user restart dank-force-press.service
        printf 'dank-force-press.service enabled and (re)started\n'
    elif systemctl --user is-active --quiet dank-force-press.service; then
        run systemctl --user restart dank-force-press.service
        printf 'dank-force-press.service was running; restarted with the new watcher\n'
    else
        printf 'service not enabled; try a dry run first:\n'
        printf '  %s --dry-run -v\n' "$DEST_BIN"
        printf 'then: systemctl --user enable --now dank-force-press.service\n'
    fi
fi

if [[ ! -e "$UDEV_DEST" ]] || ! cmp -s "$UDEV_RULE" "$UDEV_DEST"; then
    printf '\nOptional haptic feedback (USB only) needs write access to the trackpad hidraw node:\n'
    printf '  sudo install -m 644 %s %s\n' "$UDEV_RULE" "$UDEV_DEST"
    printf '  sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=hidraw\n'
fi
