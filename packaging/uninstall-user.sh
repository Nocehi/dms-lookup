#!/usr/bin/env bash
set -euo pipefail

echo "Removing dankLookup user-scope files..."
rm -f "${HOME}/.local/bin/dank-lookup-selection"
rm -rf "${HOME}/.local/lib/dank-lookup"
rm -rf "${HOME}/.config/DankMaterialShell/plugins/dankLookup"

echo
echo "dankLookup files removed."
echo "Manual cleanup still needed:"
echo "  1. Remove Mod+Alt+D bind from niri binds.kdl"
echo "  2. Remove Dank Lookup window rule from niri windowrules.kdl"
echo "  3. Remove ~/.config/dank-lookup/ if created"
echo "  4. Remove ~/.local/share/stardict/dic/ dictionaries if no longer needed"
