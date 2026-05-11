#!/usr/bin/env bash
set -euo pipefail

DEST_BIN="${HOME}/.local/bin"
DEST_LIB="${HOME}/.local/lib/dank-lookup"
DEST_PLUGIN="${HOME}/.config/DankMaterialShell/plugins/dankLookup"

echo "Installing dankLookup to user-scope paths..."
install -Dm755 bin/dank-lookup-selection "$DEST_BIN/dank-lookup-selection"
install -Dm755 lib/backends/sdcv.sh "$DEST_LIB/backends/sdcv.sh"
install -Dm644 lib/backends/mock.sh "$DEST_LIB/backends/mock.sh"
mkdir -p "$DEST_PLUGIN"
cp plugin/plugin.json "$DEST_PLUGIN/"
cp plugin/DankLookup.qml "$DEST_PLUGIN/"
cp README.md "$DEST_PLUGIN/"

echo
echo "dankLookup installed."
echo "Next steps:"
echo "  1. dms ipc call plugins reload dankLookup"
echo "  2. Add niri keybind (see docs/niri-dms-install.md)"
echo "  3. Install dictionaries (see docs/dictionary-sources.md)"
