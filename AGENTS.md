# dankLookup AGENTS.md

## Architecture

- Repo source at `~/dank-lookup/`. Deployment goes to three paths:
  - `~/.local/bin/dank-lookup-selection`
  - `~/.local/lib/dank-lookup/backends/sdcv.sh`
  - `~/.config/DankMaterialShell/plugins/dankLookup/` (QML plugin)
- QML plugin reads `/tmp/dank-lookup/latest.json` via FileView (Timer 300ms)
- `dank-lookup-selection` calls `sdcv.sh`, wraps result into `latest.json`
- sdcv.sh writes to `$out_file` (default: `/tmp/dank-lookup/backend_result.json`). Override via `DANK_LOOKUP_OUTPUT`.
- selection script reads `backend_result.json`, restructures per legacy contract, writes `latest.json`

## Versions

Implemented milestones (19 versions, v0.1.0 → v0.4.9):
- v0.1.0–v0.4.2: mock → sdcv → MD3 UI → latency → cleanup
- v0.4.3: exact/related result policy
- v0.4.4: file-based config, Python-owned sdcv subprocess calls
- v0.4.5–0.4.5a: locale-agnostic UI labels, dynamic availableModes
- v0.4.6: pill + Copy/Open-full actions for single-mode results
- v0.4.7: ja-ja (reader.dict JA) + yue-yue (words.hk) dictionary deployment
- v0.4.7b: DANK_LOOKUP_OUTPUT test isolation, ja/yue fixtures
- v0.4.8: mode routing policy, 8 reading-routing fixtures
- v0.4.9: risk hardening — classify unknown fallback, CI-minimal config, pre-release check, redistribution notes

## QML gotchas

- Use `console.warn` for debug output (`console.log` is filtered by DMS)
- Use `FileView` from `Quickshell.Io` for file reading. `XMLHttpRequest` with `file://` is blocked.
- Use `FloatingWindow` for popup windows (not `QtQuick.Window` which niri tiles as a regular window)
- `IpcHandler` type is unavailable (the `quickshell-ioplugin` shared library is missing on this system)
- Create a fresh FileView instance via `Qt.createQmlObject` per read. Reusing a persistent FileView instance returns stale content.
- Property names must not conflict with QML element IDs (`id: foo` + `property string foo` → `not an lvalue`)
- `dms ipc call plugins reload dankLookup` is the standard reload. When manifest cache is stale, `kill $(pgrep -x qs)` forces full rescan and clears QML bytecode cache.
- QML copy: `Quickshell.Io.Process` runs `wl-copy`; do not use or document
  `Qt.application.clipboard` as the current implementation.
- QML process spawn: `Qt.createQmlObject('import Quickshell.Io; Process { command: [...]; running: true }', root)`

## Backend

- `lib/backends/sdcv.sh` is the real backend. Shell classifies language, Python (via `python3 -c` heredoc) owns sdcv subprocess calls, config loading, entry ranking, and JSON assembly.
- `lib/backends/mock.sh` is pure mock fallback (no sdcv dependency).
- `~/.config/dank-lookup/config.json` is optional user config. Missing → built-in defaults. Invalid → warn + fallback to defaults.
- Current local config modes: `han`, `yue`, `ja`, `en-en` (no `han-en`)
- `DANK_LOOKUP_CONFIG` env var overrides config path (for testing).
- `DANK_LOOKUP_OUTPUT` env var overrides backend result output path (for per-test isolation).
- `STARDICT_DATA_DIR` colon-separated does NOT work for system paths (`/usr/share/stardict/dic/`). System dicts (GCIDE, WordNet) need `--data-dir` flag. User dicts use `STARDICT_DATA_DIR`.
- classify() fallback: `"unknown"` (was `"kana"` before v0.4.9). Classes: `kana`, `cjk`, `latin`, `mixed`, `unknown`.
- Installed dictionaries (8 total, user-scope + system):
  - han: 萌典重編國語辭典 (161k)
  - yue: words.hk 粵語詞典 (16k, do NOT redistribute)
  - ja: reader.dict JA (100k)
  - en-en: dictd_www.dict.org_gcide (174k), WordNet (149k)
  - bilingual: XDICT英漢辭典 (174k), XDICT漢英辭典 (158k), FreeDict+WikDict (26k)
- No `jq` on this system. Use `python3` for JSON processing.

## Testing

Fixture sets are config-sensitive. Do not summarize them as a single all-pass
number unless the exact config and suite list are included.

| Set | Count | Config | Purpose |
|-----|-------|--------|---------|
| `fixtures/` | 4 | current/default local config | Local baseline; currently `3/4` because local config routes latin to `en-en` instead of historical `han-en` |
| `fixtures-ci-minimal/` | 2 | `ci-minimal.json` | CI portable — en-en only (GCIDE+WordNet) |
| `fixtures-ja-yue/` | 6 | current config | ja/yue deployment verification |
| `fixtures-reading-routing/` | 8 | current config | CJK routing policy |
| `fixtures-risk-hardening/` | 3 | current config | unknown class, negative assertions |

Commands:

```bash
# CI baseline (portable — only GCIDE + WordNet)
DANK_LOOKUP_CONFIG=tests/configs/ci-minimal.json \
  python3 tests/run_backend_fixtures.py tests/fixtures-ci-minimal

# Local baseline with built-in defaults. This is useful for drift checks, but
# currently has a separate `為甚麼` fixture-runner decode follow-up.
DANK_LOOKUP_CONFIG=/tmp/dank-lookup-no-such-config.json \
  python3 tests/run_backend_fixtures.py tests/fixtures

# Local full verification
python3 tests/run_backend_fixtures.py tests/fixtures-ja-yue
python3 tests/run_backend_fixtures.py tests/fixtures-reading-routing
python3 tests/run_backend_fixtures.py tests/fixtures-risk-hardening
```

Per-test output isolation: each fixture writes to `/tmp/dank-lookup/fixture-{name}.json` via `DANK_LOOKUP_OUTPUT`. Tests do not share the output file, so concurrent manual lookup and test runs do not collide.

QML copy/open actions:
- Copy uses `Quickshell.Io.Process` with `wl-copy`; do not document it as
  `Qt.application.clipboard`.
- Open-full writes `/tmp/dank-lookup/full.txt` and launches `xdg-open`.
- `Process` stdin/lifecycle behavior after `p.write(...)` remains a focused
  review follow-up; verify the Quickshell API before prescribing `close()` or
  `end()`.

Pre-release hygiene:
```bash
bash packaging/pre-release-check.sh
```
Checks: no absolute paths in code, no dictionary data in repo, no runtime tmp, shell syntax, required files.

## niri integration

- Keybind: `Mod+Alt+D repeat=false` in `~/.config/niri/dms/binds.kdl`
- Floating rule: `dms/windowrules.kdl` (included by `config.kdl`)
- `niri validate -c ~/.config/niri/config.kdl` before config reload
- `niri msg action load-config-file` for live reload (may fail silently; full restart may be needed)

## Deployment

- `packaging/install-user.sh` and `packaging/uninstall-user.sh`
- No absolute `/home/nocehi` paths in repo code (use `$HOME`)
- Do not bundle dictionary data. Users install dictionaries themselves.
- Some dictionaries are non-redistributable (words.hk: ALL RIGHTS RESERVED, XDICT: unknown license). See `docs/dictionary-sources.md` for per-source redistribution status.
- Repo is source; deployment targets `~/.local/` and `~/.config/`
