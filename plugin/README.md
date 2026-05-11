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
              ├─ cooldown (700ms): multi-spawn guard
              ├─ wl-paste: primary → clipboard fallback
              ├─ backend sdcv.sh: language detect → XDICT routing
              ├─ atomic write: latest.json.tmp → mv → latest.json
              └─ debug.log: per-execution trace

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
| `~/.local/lib/dank-lookup/backends/sdcv.sh` | Backend adapter (sdcv + XDICT, 4-category language routing) |
| `~/.local/lib/dank-lookup/backends/mock.sh` | Pure mock fallback |
| `~/.config/niri/dms/binds.kdl` | Keybind |
| `~/.config/niri/dms/windowrules.kdl` | Floating window rule |
| `/tmp/dank-lookup/latest.json` | Lookup result (runtime) |
| `/tmp/dank-lookup/debug.log` | Execution log (runtime) |

## Backend

### sdcv with XDICT dictionaries (current)

The plugin now uses `sdcv --json` with two user-scope dictionaries in `~/.local/share/stardict/dic/`:

| Dictionary | Lookup direction | Word count |
|------------|-----------------|------------|
| XDICT英漢辭典 | English → Chinese (auto-selected for ASCII queries) | 174,049 |
| XDICT漢英辭典 | Chinese → English (auto-selected for non-ASCII queries) | 158,137 |
| dictd_www.dict.org_gcide | English monolingual (en-en exact / detailed Webster-style entries) | 174,222 |
| WordNet | English monolingual (en-en exact fallback / companion source) | 149,535 |

The backend automatically detects the query language and picks the right dictionary:

| Category | Detection | Primary dict | Mode |
|----------|----------|-------------|------|
| Latin (en) | ASCII alpha first char | XDICT英漢辭典 | 漢英 |
| CJK (zh) | Han characters only | 萌典重編國語辭典 + XDICT漢英辭典 | 漢語 / 漢英 |
| Mixed | CJK + Latin | unfiltered → both dicts | auto |
| Kana (ja) | Hiragana/Katakana | unfiltered → mock (JMdict planned) | — |

For CJK queries, the popup defaults to 漢語 (萌典). For Latin queries, it defaults to 漢英 (XDICT), with 英英 available as a second mode. The 英英 mode queries GCIDE and WordNet, then splits exact matches from fuzzy / nearby entries. Exact results stay in the default definition body; fuzzy entries are exposed through `related_by_mode` and the optional Related toggle.

If the primary dict returns no result, it tries the secondary, then falls back to an unfiltered `sdcv` query. Mock mode is the last resort.

> The English-中文 FreeDict dictionary (26,660 words) is also available in the unfiltered fallback path.

### Mock mode (fallback)

When no dictionary backend is installed or no definition is found, the probe returns a mock result.

## Version

| Version | Changes |
|---------|---------|
| V1 | Mock backend, `FloatingWindow` popup, `Mod+Alt+D` keybind, file-based signal |
| V2a | sdcv backend with XDICT user-scope dictionaries, 3-tier fallback |
| V2b | Language routing (latin/CJK/kana/mixed), MD3 card UI, segmented mode row, Asebi Mincho title |
| V2c | Mono dictionary routing: `results_by_mode` contract, 漢語→萌典, 漢英→XDICT, 英英→GCIDE/WordNet |
| V2d | UI polish: larger card, title inset, serif title split, taller scroll body |
| V2e | Latency and body typography: 300ms file check, 700ms cooldown, 0.25s wl-paste timeouts, body line-height |
| V2f | Source-specific cleanup: conservative GCIDE/Webster whitespace, `[1913 Webster]` cleanup, scroll overflow arrow, scroll-position reset, GCIDE/WordNet exact-match ranking |
| V0.4.3 | Result quality policy: exact entries in main body, fuzzy entries in `related_by_mode`, Related toggle |
| V0.4.4 | File-based source config support in backend, `DANK_LOOKUP_CONFIG` override, Python-owned `sdcv` subprocess planning |
| V0.4.4a | Integration fix: `latest.json` passes backend metadata through; QML resets mode state on new data |
| V0.4.4b | Backend fixture tests for routing, exact/related split, 萌典, XDICT, and user-scope `STARDICT_DATA_DIR` behavior |
| V0.4.5 | Locale-agnostic UI labels: `modeLabels` removed; labels from `results_by_mode[mode].label` |
| V0.4.5a | Config-driven segment visibility: dynamic `availableModes` Repeater model |
| V0.4.6 | Single-mode pill + Copy (clipboard) + Open-full (xdg-open) actions |
| Big Bang | Concept: jieba segmentation fallback for multi-word CJK selections |
| V0.4.7 | ja-ja (reader.dict JA) + yue-yue (words.hk) dictionary deployment; new config modes |
| V0.4.7b | Test infrastructure: `DANK_LOOKUP_OUTPUT` per-test output isolation; ja/yue optional fixture set |
| V0.4.8 | Mode routing policy: 8 reading-routing fixtures confirm CJK routing matches design principles; no backend changes needed |
| V0.4.9 | Risk hardening: classify() unknown fallback, dictionary redistribution labels, CI-minimal config, pre-release check script |
| V0.5.0 | Proposal: state-per-control + T.Templates refactor (planned) |
| V0.5.0a | Component refactor: inline `T.Button` replaces close/C/F from `Rectangle` + `MouseArea` |
| V0.5.0b | Copy action fix: `Qt.application.clipboard` → `wl-copy` via Process |

## Config

Optional config path:

```text
~/.config/dank-lookup/config.json
```

Override for testing:

```bash
DANK_LOOKUP_CONFIG=/path/to/config.json $HOME/.local/lib/dank-lookup/backends/sdcv.sh Linux
```

If no config exists, the backend uses built-in defaults matching the current local behavior.

Known orthography note:

```text
臺灣 -> 漢語 / 萌典
台灣 -> 漢英 / XDICT漢英辭典
```

## Backend Fixtures

Run:

```bash
$HOME/.local/lib/dank-lookup/tests/run_backend_fixtures.py
```

Current local result:

```text
4/4 passed
```

The fixture runner is sequential because the backend writes a shared runtime file at `/tmp/dank-lookup/backend_result.json`.

The default fixture suite is expected to pass with built-in/default config. A deliberately restrictive config can fail these fixtures; that confirms config filtering is active.

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

All processing is local. No network requests. No clipboard monitoring. No daemon processes.

## Dictionaries

Dank Lookup does not ship dictionary data. Some dictionaries that work locally may not be redistributable. Install dictionaries yourself and check each source's license before sharing converted StarDict files. See `docs/dictionary-sources.md` for a full list with redistribution status.
