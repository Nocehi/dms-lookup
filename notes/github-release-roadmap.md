# dankLookup GitHub release roadmap

用途：整理如果要把 dankLookup 發佈成 GitHub 倉庫，需要補齊的工程、授權、隱私、安裝和維護工作。這不是立即發佈清單，而是從本機原型走到公開 repo 的路線圖。

## Current local state

本機已有：

- DMS plugin: `~/.config/DankMaterialShell/plugins/dankLookup/`
- selection script: `~/.local/bin/dank-lookup-selection`
- backend adapter: `~/.local/lib/dank-lookup/backends/sdcv.sh`
- mock fallback: `~/.local/lib/dank-lookup/backends/mock.sh`
- niri keybind: `Mod+Alt+D`
- niri floating rule for `dev.noctalia.noctalia-qs` title `Dank Lookup`
- user-scope StarDict dictionaries under `~/.local/share/stardict/dic/`
- current local backend includes:
  - `XDICT英漢辭典`
  - `XDICT漢英辭典`
  - `萌典重編國語辭典`
  - `dictd_www.dict.org_gcide`
  - `WordNet`
- current local runtime includes:
  - file polling at `300ms`
  - script cooldown at `700ms`
  - `wl-paste` timeout at `0.25s`
  - `results_by_mode` routing for `漢語 / 漢英 / 英英`
  - conservative GCIDE/Webster cleanup
  - scroll overflow affordance
  - scroll-position reset on new lookup / mode switch
  - GCIDE + WordNet exact-match ranking for `en-en`

## Release principle

The public repo must ship code, not dictionary data.

Do not include:

- XDICT dictionary files
- FreeDict files
- JMdict / EDICT converted data
- CC-CEDICT converted data
- any commercial or unclear StarDict package
- `/tmp/dank-lookup` runtime output

The repo may include:

- QML plugin code
- shell scripts
- backend adapter code
- README
- install instructions
- dictionary compatibility notes
- sample JSON fixtures with invented content

## Proposed repo layout

```text
dank-lookup/
  README.md
  LICENSE
  CHANGELOG.md
  docs/
    materialdesign3manual.md
    dictionary-sources.md
    niri-dms-install.md
    privacy.md
  plugin/
    plugin.json
    DankLookup.qml
  bin/
    dank-lookup-selection
  lib/
    backends/
      sdcv.sh
      mock.sh
  examples/
    latest.sample.json
    backend_result.sample.json
  packaging/
    install-user.sh
    uninstall-user.sh
```

## Naming cleanup

Already done locally:

- `sdcv.sh` is the real backend adapter.
- `mock.sh` remains a pure fallback/sample backend.
- `dank-lookup-selection` calls `sdcv.sh`.

Before public release:

- remove any stale README wording that calls `mock.sh` the real backend
- keep compatibility note in changelog if early users saw the old layout

## License plan

Recommended:

- Plugin/script code: MIT or Apache-2.0.
- Do not license third-party dictionaries in this repo because they are not included.
- Add `docs/dictionary-sources.md` listing known compatible dictionaries and their upstream licenses.

README wording:

```text
This project does not distribute dictionary data. Users are responsible for installing dictionaries whose licenses permit their intended use.
```

## Privacy plan

Default behavior:

- local selection read via `wl-paste`
- local dictionary lookup via `sdcv`
- no network request
- no daemon
- no clipboard monitoring loop
- no bundled telemetry

Important wording:

```text
Network dictionary, translation, OCR, or LLM backends must be opt-in and disabled by default.
```

## Install strategy

V1 public install should be user-scope only:

```bash
install -Dm755 bin/dank-lookup-selection ~/.local/bin/dank-lookup-selection
install -Dm755 lib/backends/sdcv.sh ~/.local/lib/dank-lookup/backends/sdcv.sh
mkdir -p ~/.config/DankMaterialShell/plugins/dankLookup
cp -r plugin/* ~/.config/DankMaterialShell/plugins/dankLookup/
```

Do not edit niri config automatically in the first release. Provide manual snippet:

```kdl
Mod+Alt+D repeat=false { spawn "/home/$USER/.local/bin/dank-lookup-selection"; }

window-rule {
    match app-id="dev.noctalia.noctalia-qs" title="Dank Lookup"
    open-floating true
}
```

## Dictionary setup docs

Document user-scope path:

```bash
mkdir -p ~/.local/share/stardict/dic
sdcv --list-dicts
```

Document supported current dictionaries:

```text
XDICT英漢辭典
XDICT漢英辭典
萌典重編國語辭典
dictd_www.dict.org_gcide
WordNet
English-中文 FreeDict+WikDict dictionary (unfiltered fallback)
```

Do not provide direct bundled download unless license is verified and redistribution is clearly permitted.

## Version roadmap

### v0.1.0: local MVP

- file-based signal
- DMS popup
- `Mod+Alt+D`
- mock backend

### v0.2.0: sdcv + XDICT

- user-scope `sdcv`
- XDICT Traditional Chinese priority
- FreeDict unfiltered fallback
- no bundled dictionaries

### v0.3.0: Material 3 UI

- card layout cleanup
- Asebi Mincho query title
- segmented dictionary mode row
- source label
- scroll body validation

### v0.4.0: multi-source backend

- `en-en` GCIDE/Webster
- `zh-en` / `en-zh` routing
- JSON schema with `available_modes`

### v0.4.1: latency and typography polish

- 300ms local file check while popup is hidden
- 700ms hotkey cooldown
- 0.25s `wl-paste` primary / clipboard timeouts
- CJK / Latin body line-height tuning

### v0.4.2: source-specific cleanup

- conservative GCIDE/Webster whitespace cleanup
- reduce repeated standalone `[1913 Webster]` markers
- preserve headword, pronunciation, numbered senses, examples
- scroll overflow affordance
- reset body scroll position on new lookup and mode switch
- rank exact English monolingual matches before fuzzy / nearby entries
- include GCIDE and WordNet in `en-en` when useful

### v0.4.3: result quality policy

- exact entries stay in the default `results_by_mode` body
- fuzzy / nearby entries move to `related_by_mode`
- QML exposes related entries through a compact Related toggle
- `Linux` shows WordNet exact in the main body and GCIDE fuzzy entries only in related
- add backend fixture tests for `Linux`, `request`, `為甚麼`, and `consolidate`

### v0.4.4: file-based source config

- backend supports optional `~/.config/dank-lookup/config.json`
- `DANK_LOOKUP_CONFIG=/path/to/config.json` overrides config path for tests
- missing config uses built-in defaults
- Python now owns source planning and `sdcv` subprocess calls
- Python subprocess calls set `STARDICT_DATA_DIR` for user-scope dictionaries when no per-source `data_dir` is provided

### v0.4.4a: integration metadata passthrough

- `dank-lookup-selection` forwards `related_by_mode`, `mode_preferences`, and `config`
- QML resets mode state on every new lookup
- prevents stale tab/body state when switching from full results to mock/no-result cases

### v0.4.4b: backend fixtures

- installed `~/.local/lib/dank-lookup/tests/run_backend_fixtures.py`
- installed four local fixtures: `Linux`, `request`, `為甚麼`, `consolidate`
- current local result: `4/4 passed`
- restrictive custom config expectedly fails the default fixture suite, confirming config filtering works
- future fixture candidate: `臺灣` / `台灣` orthography behavior

### v0.4.5: locale-agnostic UI labels

- QML `modeLabels` hardcoded table removed; segmented row reads labels from `results_by_mode[mode].label`
- `~/.config/dank-lookup/config.json` can supply per-mode labels (e.g., `"Han"`, `"En-En"`, etc.)
- Fallback to mode key (`han`/`han-en`/`en-en`) when no label is present
- Backend built-in defaults still provide Traditional Chinese labels for out-of-the-box experience

### v0.4.5a: config-driven segment visibility

- Repeater `model` changed from static `["han","han-en","en-en"]` to dynamic `availableModes`
- Segment width scales to `availableModes.length` instead of constant `/3`
- Config-disabled modes disappear entirely; query-level missing results still filtered by backend `available_modes`

### v0.4.6: pill + utility actions

- Single-mode pill replaces empty segmented row when only one mode is available (QML only)
- Copy action copies `resultBody` to clipboard via `wl-copy` (Quickshell.Io.Process)
- Open-full action writes body to `/tmp/dank-lookup/full.txt` and opens with `xdg-open`
- Segmented row preserved when multiple modes are available; hidden for single-mode cases
- Mode pill shows active mode label; C/F action buttons use simple text labels

### Big Bang concept: CJK segmentation fallback

- Concept only; no implementation yet
- When exact lookup fails for multi-character CJK text, segment into tokens via jieba (MIT)
- Per-token dictionary lookup as opt-in fallback
- Inspired by Smartisan Big Bang

### v0.4.7: ja-ja and yue-yue dictionary deployment

- Deployed `reader.dict JA` (100,036 words, ja-ja monolingual) to user-scope StarDict
- Converted words.hk CSV (16,203 OK+已公開 entries) to StarDict via pyglossary
- Added `ja` mode (kana default, ja-ja source) and `yue` mode (cjk default, yue-yue source) to config
- New modes coexist with existing han/han-en/en-en without breaking core fixtures

### v0.4.7b: test infrastructure cleanup

- Backend supports `DANK_LOOKUP_OUTPUT` env var for per-test output path isolation
- Fixture runner uses per-test output files, eliminating shared-file race condition
- Added optional `fixtures-ja-yue/` set (6 fixtures) for ja/yue dictionary verification
- Core fixture suite unchanged: 4/4 passes with default config

### v0.4.8: mode routing and source quality

- Added `fixtures-reading-routing/` (8 fixtures) documenting real CJK reading behavior
- Confirmed current routing matches design principles: han default for generic CJK, ja default for kana and Japanese words, yue available for Cantonese exact matches
- No backend routing changes needed — current behavior already aligns with v0.4.8 policy
- Open questions documented: Cantonese marker promotion, ja default_for scope, explicit mode_order

### v0.4.9: risk hardening

- `dictionary-sources.md` updated with redistribution status per source (redistributable / user-installed only / do not redistribute)
- README and plugin README include release safety notes on dictionary redistribution
- `classify()` fallback changed from `"kana"` to `"unknown"` — prevents emoji/numbers/punctuation misclassification as Japanese
- Added `fixtures-risk-hardening/` (3 fixtures) asserting unknown text does not default to ja
- Extended fixture runner with `default_mode_not` assertion
- Added `tests/configs/ci-minimal.json` — GCIDE + WordNet only, portable CI baseline
- Added `tests/fixtures-ci-minimal/` — en-en-only portable fixture subset (Linux, request)
- Added `packaging/pre-release-check.sh` — automated hygiene check (5 checks, read-only)

### v0.5.0a: QML button refactor (close + pill actions)

- Closed button, Copy button, and Open-full button migrated from `Rectangle + MouseArea` to inline `T.Button` (QtQuick.Templates)
- `import QtQuick.Templates as T` added to `DankLookup.qml`
- cursorShape handled via transparent `MouseArea` (`acceptedButtons: Qt.NoButton`)
- Quickshell does not support local file imports (`import "."`), so components are inline rather than separate QML files
- Residual `DankIconButton.qml` / `DankPillActionButton.qml` removed in post-cleanup

### v0.5.0b: copy action fix

- `Qt.application.clipboard.text = resultBody` → `Process { command: ["wl-copy"] }` + `p.write(resultBody)`
- Uses same `Quickshell.Io.Process` pattern as `openFullSource()`
- Eliminates journal TypeError (`Value is undefined`)

### v0.5.0: monolingual-only direction + QML refactor

Bilingual translation dictionaries (JMdict/EDICT, CC-CEDICT, CC-Canto) are
not being pursued in the current local prototype. The project direction favors
monolingual sources:

- han → 萌典
- yue → words.hk
- ja → reader.dict JA
- en-en → GCIDE + WordNet

Bilingual lookup was already proven feasible in earlier versions:

- v0.2.0: XDICT英漢辭典 / XDICT漢英辭典 (han-en mode)
- v0.4.0: han-en routing via language classification

These remain available in the built-in backend defaults and can be re-enabled
via config. The decision not to add more bilingual sources is a local
experiment preference, not an architectural limitation.

## Review checklist

- [x] Live plugin and repo copy are in sync.
- [x] README and roadmap describe `wl-copy` copy behavior, not `Qt.application.clipboard`.
- [x] v0.5.0a docs mention the inline `T.Button` outcome.
- [x] Copy action is verified by clicking `C` and checking `wl-paste`.
- [x] No new `dankLookup` TypeError appears in `journalctl --user`.
- [x] No clipboard polling is introduced.
- [x] No network lookup is introduced by default.
- [x] `/tmp/dank-lookup/latest.json` privacy behavior is documented.
- [x] `/tmp/dank-lookup/full.txt` behavior for Open-full is documented.
- [x] Backend fixtures still pass after any implementation fix.

## Pre-release checklist

- No absolute `/home/nocehi` paths in repo code.
- No dictionary files in repo.
- No `/tmp/dank-lookup/*.json` runtime data in repo.
- Shell scripts pass `bash -n`.
- README has install, uninstall, privacy, dictionary setup.
- Screenshots do not expose private clipboard text.
- License file present.
- Changelog present.
- Known limitations documented.

## Known limitations to publish

- Wayland cannot read arbitrary app text; user must select text first.
- Primary selection behavior varies across applications.
- Clipboard fallback may return stale clipboard content if no primary selection exists.
- niri/DMS integration is the primary target; other compositors are untested.
- `sdcv` output quality depends on dictionary data.
