# Dank Lookup — DMS Plugin

Local selection dictionary lookup popup for Dank Material Shell. Inspired by macOS Look Up.

## Requirements

- [Dank Material Shell](https://github.com/AvengeMedia/DankMaterialShell) >= 1.2.0
- `wl-paste` (from `wl-clipboard`)
- A dictionary backend (optional; mock mode works without one)

## Installation

```bash
dms ipc call plugins reload dankLookup
dms ipc call plugins list  # Verify: dankLookup [loaded]
```

Also install the selection probe script:

```bash
chmod +x $HOME/.local/bin/dank-lookup-selection
```

## Usage

1. Select text in any application.
2. Press **Mod+Alt+D** or run:
   ```bash
   $HOME/.local/bin/dank-lookup-selection
   ```
3. The popup card appears automatically via a low-latency local file signal.
4. Press **Escape** or click **✕** to close.

## Architecture

```
Mod+Alt+D → spawn dank-lookup-selection
              ├─ wl-paste: primary → clipboard fallback
              ├─ backend sdcv.sh: language classify → mode routing
              └─ atomic write: latest.json.tmp → mv → latest.json

DankLookup.qml (Timer 300ms, popup-hidden only)
  └─ FileView (Qt.createQmlObject, fresh per read)
       └─ timestamp change? → show FloatingWindow
            ├─ reset Flickable.contentY on new data / mode switch
            └─ Escape / ✕ → hide
```

The popup card follows Material Design 3 conventions: filled card surface, outline border, segmented dictionary mode row (漢語/漢英/英英), Asebi Mincho serif title for CJK queries.

## Keybind

```kdl
Mod+Alt+D repeat=false { spawn "$HOME/.local/bin/dank-lookup-selection"; }
```

Located in `~/.config/niri/dms/binds.kdl`.

## Niri window rule

```kdl
window-rule {
    match app-id="dev.noctalia.noctalia-qs" title="Dank Lookup"
    open-floating true
}
```

niri floats and positions the popup naturally. Located in `~/.config/niri/dms/windowrules.kdl` (included by config.kdl).

## Files

| Path | Purpose |
|------|---------|
| `~/.config/DankMaterialShell/plugins/dankLookup/` | Plugin directory |
| `~/.local/bin/dank-lookup-selection` | Selection probe script |
| `~/.local/lib/dank-lookup/backends/sdcv.sh` | Backend adapter |
| `~/.local/lib/dank-lookup/backends/mock.sh` | Pure mock fallback |
| `~/.config/niri/dms/binds.kdl` | Keybind |
| `~/.config/niri/dms/windowrules.kdl` | Floating window rule |
| `/tmp/dank-lookup/latest.json` | Lookup result (runtime, tmpfs) |

## Backend

The backend uses `sdcv --json` with user-scope and system dictionaries. Language is auto-detected and routed to the appropriate mode:

| Class | Detection | Default mode |
|-------|----------|--------------|
| CJK | Han characters | 漢語 (萌典 + CC-CEDICT) |
| Kana | Hiragana / Katakana | ja (JMdict + reader.dict) |
| Latin | ASCII alpha first char | han-en (XDICT英漢) |
| Mixed | CJK + Latin | han-en |
| Unknown | Emoji / punctuation / numbers | — (mock fallback) |

Supported dictionaries (user installs separately — see `docs/dictionary-sources.md`):

| Dictionary | Mode | Words |
|------------|------|-------|
| 萌典重編國語辭典 | han | 161,194 |
| CC-CEDICT 漢英辭典 | han | 122,437 |
| words.hk 粵語詞典 | yue | 16,204 |
| reader.dict JA | ja | 100,036 |
| JMdict | ja | 412,413 |
| XDICT英漢辭典 | han-en | 174,049 |
| XDICT漢英辭典 | han-en | 158,137 |
| dictd_www.dict.org_gcide | en-en | 174,222 |
| WordNet | en-en | 149,535 |
| English Wiktionary | en-en | 1,306,573 |
| FreeDict+WikDict eng-zho | han-en fallback | 26,660 |

Multi-word CJK phrases with no direct match are split into tokens by the optional `dank-lookup-tokenize` Rust binary (jieba-rs + lindera) and looked up per-token.

### Mock mode (fallback)

When no dictionary backend is installed or no definition is found, the probe returns a mock result.

## Config

Optional config path:

```text
~/.config/dank-lookup/config.json
```

Override for testing:

```bash
DANK_LOOKUP_CONFIG=/path/to/config.json $HOME/.local/lib/dank-lookup/backends/sdcv.sh Linux
```

If no config exists, the backend uses built-in defaults. See `docs/json-schema.md` for the full output contract.

## Backend Fixtures

Run from the repo root after installing:

```bash
# CI baseline (portable — only needs GCIDE + WordNet)
DANK_LOOKUP_CONFIG=tests/configs/ci-minimal.json \
  python3 tests/run_backend_fixtures.py tests/fixtures-ci-minimal

# Full local suites
python3 tests/run_backend_fixtures.py tests/fixtures
python3 tests/run_backend_fixtures.py tests/fixtures-ja-yue
python3 tests/run_backend_fixtures.py tests/fixtures-reading-routing
python3 tests/run_backend_fixtures.py tests/fixtures-risk-hardening
python3 tests/run_backend_fixtures.py tests/fixtures-segmentation
```

Each fixture writes to its own output file via `DANK_LOOKUP_OUTPUT` — concurrent runs do not collide.

## Uninstall

```bash
rm -rf $HOME/.config/DankMaterialShell/plugins/dankLookup
rm -f $HOME/.local/bin/dank-lookup-selection
rm -rf $HOME/.local/lib/dank-lookup
rm -rf /tmp/dank-lookup
dms ipc call plugins reload
```

Also remove the keybind from `~/.config/niri/dms/binds.kdl` and the window rule from `~/.config/niri/dms/windowrules.kdl`.

## Privacy

All processing is local. No network requests. No clipboard monitoring. No daemon processes. See `docs/privacy.md`.

## Dictionaries

Dank Lookup does not ship dictionary data. Some dictionaries that work locally may not be redistributable. Install dictionaries yourself and check each source's license before sharing converted StarDict files. See `docs/dictionary-sources.md` for a full list with redistribution status.
