# niri installation and optional DMS compatibility

Dank Lookup is standalone-first. Dank Material Shell is not required for the
normal selection → popup → local dictionary path.

## Prerequisites

- niri (or another Wayland compositor);
- Quickshell 0.3.x (or the noctalia-qs fork) with `IpcHandler` support and
  Qt 6.9 or newer (`QtQuick.Effects.RectangularShadow`);
- `wl-paste` and `wl-copy` from `wl-clipboard`;
- Python 3, `sdcv`, and any dictionaries you are licensed to use;
- `xdg-open` for the optional Open full action.

## Install

```bash
bash packaging/install-user.sh --with-tokenizer
```

The installer requires one tokenizer choice: `--with-tokenizer` (build with
cargo), `--tokenizer-bin PATH`, or `--without-tokenizer` (bigram fallback). It
stops before installing anything if the installed tokenizer is missing or
predates the stdin CLI.

The installer places a named `dank-lookup` Quickshell configuration, local
backend, default han/en/ja/yue routing config (only when absent), language-pack
status tool, and a user service. It does not install dictionary data or replace
an existing user config.

The selection command can start the named Quickshell configuration on demand.
To avoid a cold frontend process launch, enable the resident frontend for the
graphical session:

```bash
systemctl --user daemon-reload
systemctl --user enable --now dank-lookup.service
```

The resident frontend exposes explicit IPC only. It does not monitor or read
the clipboard.

## niri keybind

Add this bind to the appropriate niri config fragment:

```kdl
Mod+Alt+D repeat=false { spawn-sh "$HOME/.local/bin/dank-lookup-selection"; }
```

The script first asks the resident frontend to render its loading frame, then
reads the primary selection (clipboard fallback) once and submits the query.

## Floating rule

Use the popup title as the stable match:

```kdl
window-rule {
    match title="Dank Lookup"
    open-floating true
}
```

The former `dev.noctalia.noctalia-qs` rule is obsolete. Current local DMS source
uses other identifiers, but the sandbox audit could not observe the popup's
actual runtime app-id; this documentation therefore does not replace one
unverified hard-coded app-id with another.

Validate your complete niri config before reloading it:

```bash
niri validate -c "$HOME/.config/niri/config.kdl"
```

## Optional DMS wrapper

The compatibility wrapper is useful only when DMS theme tokens or an existing
DMS route are desired. The locally verified compatibility floor is DMS 1.4.6;
lower versions are not claimed compatible without a runtime matrix.

```bash
bash packaging/install-user.sh --with-dms
dms ipc plugins reload dankLookup
```

The reload command is an instruction, not proof that a live deployment was
reloaded. The wrapper is schema type `daemon`, not a launcher provider, and has
the distinct IPC target `dank-lookup-dms`.

To route the selection command to it explicitly:

```bash
DANK_LOOKUP_FRONTEND=dms "$HOME/.local/bin/dank-lookup-selection"
```

The wrapper declares process use because lookup, Copy, and Open full spawn local
commands. Current DMS treats that declaration as disclosure rather than an OS
sandbox.

## Smoke check

1. Run `dank-lookup-language-packs status` and note which modes are available.
2. Select known text in any application.
3. Press `Mod+Alt+D`; a loading card should appear before lookup completes.
4. Trigger a second query while the card is open; it should replace the active
   request without closing or waiting two seconds.
5. Press Escape or close the card.
