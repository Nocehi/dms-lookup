# niri + DMS installation

## Prerequisites

- [niri](https://github.com/YaLTeR/niri) (Wayland compositor)
- [Dank Material Shell](https://github.com/AvengeMedia/DankMaterialShell) >= 1.2.0
- `wl-clipboard` (provides `wl-paste`)
- `sdcv` and dictionaries (see `dictionary-sources.md`)

## Plugin installation

```bash
install -Dm755 bin/dank-lookup-selection ~/.local/bin/dank-lookup-selection
install -Dm755 lib/backends/sdcv.sh ~/.local/lib/dank-lookup/backends/sdcv.sh
mkdir -p ~/.config/DankMaterialShell/plugins/dankLookup
cp plugin/* ~/.config/DankMaterialShell/plugins/dankLookup/
dms ipc call plugins reload dankLookup
dms ipc call plugins list  # Verify: dankLookup [loaded]
```

## niri keybind

Add to `~/.config/niri/dms/binds.kdl` (or `config.kdl` directly):

```kdl
Mod+Alt+D repeat=false { spawn "$HOME/.local/bin/dank-lookup-selection"; }
```

## niri window rule

Add to `~/.config/niri/dms/windowrules.kdl`:

```kdl
window-rule {
    match app-id="dev.noctalia.noctalia-qs" title="Dank Lookup"
    open-floating true
}
```

The file must be included in `config.kdl`:

```kdl
include "dms/windowrules.kdl"
```

## Reload config

```bash
niri validate -c ~/.config/niri/config.kdl
niri msg action load-config-file
```

If `load-config-file` fails due to socket permission, log out and back in.

## Verification

1. Select text in any application.
2. Press `Mod+Alt+D`.
3. A floating popup should appear with the dictionary definition.
4. Press Escape or click ✕ to close.
