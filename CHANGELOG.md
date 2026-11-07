# Changelog

## Unreleased — standalone-first 0.8.0

- Drop the Copy button from dictionary results: copy from the right-click
  menu on the definition or with Ctrl+C (same payload). Local detections keep
  their per-value Copy buttons.
- Add local data detectors (`lib/dank_lookup_detectors.py`): when the whole
  selection is a date, timestamp, URL, e-mail, unit quantity, colour or
  prefixed integer, the result carries an optional `detections` array and the
  card shows a **Local detection** chip with a Copy per detected value.
  Calendars: 民國, 和曆, 干支, ISO week. Pure stdlib computation, enforced by an
  import-boundary test; configurable under `detectors` in `config.json`.
- Add an experimental, opt-in Magic Trackpad force-press trigger
  (`bin/dank-force-press-watch`, `systemd/dank-force-press.service`,
  `packaging/install-force-touch.sh`, haptic udev rule). Not installed or
  enabled by `install-user.sh`. Haptics work over USB and Bluetooth (the
  Bluetooth report is wrapped in report ID `0xF2`); a press that moves more
  than 3 mm is treated as a drag and does not trigger. Thresholds (160 /
  80 ms) are backed by natural-use margin measurements over both transports.
  See `docs/force-touch.md`.
- Make the clipboard fallback opt-in (`"selection": {"clipboard_fallback":
  true}` in `config.json`, default off). The `force-press` source never reads
  the clipboard. The capture command now receives the source name as its
  second argument.
- Card redesign, measured against DMS 2.0 and macOS Look Up:
  - blur behind the card (`Quickshell.Wayland` background effect) and DMS-style
    buttons; Chinese UI strings; tonal 完整條目 (Open full);
  - every exact entry is shown under a grey source heading with a hairline;
    the pronunciation sits under the headword;
  - dictionary styling reaches the card as text runs (bold, italic, grey) from
    HTML dictionaries plus conservative line rules for plain ones, with
    per-dictionary formatters for 王力, Wiktionary, GCIDE and JMdict; ordered
    lists are numbered; hard-wrapped plain dictionaries are reflowed for
    display only, so Copy still returns the dictionary text unchanged;
  - mode switch moved to quiet capsules at the bottom left; Exact/Related is
    one 相關 toggle;
  - default size 400x420 like macOS Look Up, configurable with
    `DANK_LOOKUP_CARD_WIDTH`/`DANK_LOOKUP_CARD_HEIGHT`; opt-in placement under
    the pointer with `DANK_LOOKUP_PLACEMENT=pointer` (default stays centred);
  - the full matugen palette is loaded and retried when the theme file
    appears. See `docs/theming.md`.
- Add a standalone Quickshell frontend with no `qs.*`/DMS imports; retain DMS
  only as an optional 1.4.6+ `daemon` theme/IPC wrapper.
- Replace synchronous `/tmp` file polling with two-stage Quickshell IPC:
  loading frame first, then explicit selection capture and asynchronous lookup.
- Add request UUID/sequence ordering, stale-completion suppression, popup-open
  updates, structured errors, and remove the QML two-second debounce.
- Fix Copy/Open full to use the active exact/related/token result. Previews have
  explicit truncation; bounded complete content is carried once, while an
  oversized body uses an opaque ref and is re-fetched only after Open full.
- Consume ordered segmentation results in the UI, including duplicate token
  text, with predictable mode/related/scroll reset.
- Extract the Python lookup core from the shell heredoc; retain sdcv/StarDict as
  the baseline backend and add deterministic core/runtime/CLI tests.
- Make script-feature routing character-order independent. Kana/Cantonese
  markers boost `ja`/`yue` defaults without hiding other exact modes.
- Ship fresh `han/en/ja/yue` routing, dictionary availability detection, and a
  no-download language-pack manifest/status tool. No dictionary payload is
  bundled; unresolved licenses remain unverified/non-redistributable.
- Move explicit runtime artifacts to private XDG runtime storage with 0700/0600,
  `umask 077`, atomic writes, cleanup, and a verified per-user `/tmp` fallback.
  Normal lookups retain no query/result file; explicit Open files have automatic
  short-TTL cleanup.
- Add built-in light/dark palettes, generic matugen JSON templates/safe reload,
  and a minimal DMS theme adapter.
- Add guarded standalone-first installer/uninstaller, optional DMS install,
  user service, fresh-HOME release gates, backend strategy benchmark JSON, and
  isolated offscreen QML behavior/latency harnesses.

### Post-rewrite regression fixes (fix/post-rewrite-regressions)

Measured against v0.7.1 with the same user config and dictionaries; see the
commit messages for evidence.

- sdcv: abort homograph choice prompts (`為` in words.hk) instead of waiting
  for the 1.5 s source timeout, then fetch every homograph entry.
- Report modes and segmentation tokens cut short by the lookup budget:
  `incomplete_modes` now also covers tokens, new `incomplete_tokens`, and the
  card names timed-out tokens ("Timed out: 我們, 去") in the error colour.
- Version-1 configs keep v0.7.1's Related row (`allow_related` shows fuzzy
  entries only from a dictionary without the headword: Linux → Linum).
- Tokenizer: name a pre-stdin (v0.7.1) tokenizer as `tokenizer_incompatible`;
  the installer now stops before changing anything when the tokenizer is
  missing or pre-stdin and asks for `--with-tokenizer`, `--tokenizer-bin PATH`
  or `--without-tokenizer`; it never overwrites an unmanaged tokenizer.
- Speed: exact-only sources no longer fetch fuzzy candidates they discard,
  and every sdcv query of a lookup (exact, homograph choices, fuzzy list,
  candidates, every segmentation token) runs through one stdin session per
  source (`SdcvSession`; one `--exact-search` process serves exact and fuzzy
  frames). Long sentences: ~1.3 s (v0.7.1) → ~0.35-0.40 s with every token
  filled; single words at or below v0.7.1. Results are byte-identical.
- HTML detection follows sdcv into data-dir subdirectories (any depth,
  symlinks; loops visited once), so `sametypesequence=h` dictionaries kept in
  subfolders (王力, CC-CEDICT, reader.dict) no longer show raw markup.
- Design tokens: every colour, radius, spacing step, type size, font,
  surface/outline opacity, elevation and window metric lives in
  `qml/LookupTheme.qml`, shared by standalone and the DMS wrapper (DMS 2.0
  popout baseline: radii 12/18/24, type 12/15/17/21/29, surface x0.9, 1 px
  outline x0.35, level-2 shadow). CJK headwords use Jigmo (Asebi Mincho maps
  rare URO code points to empty glyphs), other headings Asebi Mincho, UI
  MiSans TC. The DMS wrapper maps raw `Theme.outline` (outlineMedium was
  invisible at the default outline opacity). Matugen templates carry every
  palette token plus `{{mode}}`; `docs/theming.md` documents the
  `config.toml` section. A lint test rejects literals outside the token file.
- Test harnesses: offscreen Quickshell launches detect `--allow-duplicate`
  support (absent in quickshell 0.3.x) and drop desktop platform themes;
  new offscreen card screenshot harness; fixtures may declare
  config-conditioned expectations (`expect_by_config`, with a reason), used
  for the 俳句 default mode and a v2 Linux Related-row fixture.

Historical entries below record what was claimed or observed in the local
development environment at that time. Counts such as `26/26` or `93%` are not
current portable release guarantees; current suites require their exact config,
dictionaries, and reported environment.

## v0.7.1 (2026-05-12)

- Remove 700ms cooldown from `bin/dank-lookup-selection`; niri `repeat=false` is the sole anti-rapid-fire mechanism
- Archive touchpad trigger route: libinput misclassifies rapid 1/2-finger taps as 4-finger tap — cooldown only suppresses repeats, cannot prevent first false positive (see `notes/gesture-trigger-exploration.md`)
- Remove `trigger_source` argument (`tap`/`kbd`/`any`) and per-source cooldown file (`cooldown.tap`)
- Remove `debug.log` writes from `bin/dank-lookup-selection` (privacy consistency)
- Add `docs/json-schema.md`: formal contract for `backend_result.json` and `latest.json`
- Sync `plugin/plugin.json` version to 0.7.1, author to Nocehi

## v0.7.0 (2026-05-12)

- CJK phrase segmentation fallback: when a phrase has no direct dictionary entry, the optional `dank-lookup-tokenize` binary splits it into tokens and looks up each independently; first token with results fills the popup (QML backward compatible)
- New JSON keys: `segmented` (bool), `tokens` (array), `results_by_token` (dict)
- `bin/dank-lookup-selection` forwards segmentation keys
- Rust tokenizer binary at `tokenizer/` (jieba-rs 0.9 + lindera 3.0/ipadic embedded); benchmark: ZH diff=0.0% vs Python jieba, JA 10.3% higher hit rate
- New fixture suite `tests/fixtures-segmentation/` (3 fixtures); 26/26 total pass
- `tests/run_backend_fixtures.py`: `tokens_contains` + `tokens_length_gte` assertions

## v0.6.0 (2026-05-12)

- Dictionary coverage audit with `tests/coverage_probe.py` (12 suites, 188 terms)
- ja mode coverage: JMdict deployed (412k entries, CC-BY-SA 4.0), verbs/hiragana 0%→100%
- han mode: CC-CEDICT deployed (122k entries, both trad/simp idx entries), simplified Chinese 0%→100%
- en-en mode: English Wiktionary deployed (1.3M entries, truncation at 4000/12000 chars), modern tech terms now covered
- han-en mode enabled in local config for mixed-script queries
- `build_body()` in sdcv.sh: per-entry truncation (4000 chars) + total body limit (12000 chars) for Wiktionary noise control
- Fixture updates: 3 reading-routing fixtures reflect expanded cross-mode coverage from JMdict + CC-CEDICT
- Overall coverage: 82% → 93% (174/188 terms across all suites)

## v0.5.0e (2026-05-11)

- Backend no-sdcv fallback (`lib/backends/sdcv.sh`): query now passes via `sys.argv[1]` instead of shell-quoting into the Python heredoc
- Closes a localised quote-injection edge case in the mock path when `sdcv` is not installed and the query contains a single quote
- Main code path was already argv-based; this brings the fallback branch in line

## v0.5.0d (2026-05-07)

- QML Process lifecycle fix: `copyToClipboard()` and `openFullSource()` now close stdin via `stdinEnabled=false` after `p.write()`
- Quickshell Process API verified: `stdinEnabled` setter closes stdin channel, propagating EOF to subprocess

## v0.5.0c (2026-05-07)

- Review prep: v0.5.0a implementation note added, live/repo sync verified
- plugin README `/home/nocehi` paths replaced with `$HOME` for general release
- Copy action positively verified via `wl-paste`
- Review checklist appended to roadmap

## v0.5.0b (2026-05-07)

- Copy action fix: `Qt.application.clipboard.text` → `wl-copy` via `Quickshell.Io.Process`
- Quickshell DMS runtime does not expose Qt.application.clipboard

## v0.5.0a (2026-05-07)

- QML button refactor: close (✕), Copy (C), Open-full (F) migrated from `Rectangle + MouseArea` to inline `T.Button` (QtQuick.Templates)
- `cursorShape` compatibility: transparent `MouseArea` shim with `acceptedButtons: Qt.NoButton`
- `import QtQuick.Templates as T` added to DankLookup.qml
- Attempted standalone helper QML files (`DankIconButton.qml`, `DankPillActionButton.qml`) removed after local type-resolution failures

## v0.5.0 (2026-05-07)

- Monolingual-only direction documented: project favors single-language dictionaries over bilingual translation pairs
- Bilingual lookup (XDICT) proven feasible in v0.2.0/v0.4.0, remain in built-in backend defaults
- QML refactor proposal: state-per-control + T.Templates (full plan, not fully implemented)

## v0.4.9 (2026-05-07)

- classify() fallback: `"kana"` → `"unknown"` — emoji/numbers/punctuation no longer misclassified as Japanese
- `dictionary-sources.md` updated with redistribution status per source
- `fixtures-risk-hardening/` (3 fixtures) asserting unknown text does not default to ja
- `tests/configs/ci-minimal.json` — portable CI baseline (GCIDE + WordNet only)
- `tests/fixtures-ci-minimal/` — en-en-only portable fixture subset
- `packaging/pre-release-check.sh` — automated hygiene check (5 checks)

## v0.4.8 (2026-05-07)

- Mode routing fixture set: `fixtures-reading-routing/` (8 fixtures) confirming CJK routing matches design principles
- No backend routing changes needed — current behavior already aligns with v0.4.8 policy

## v0.4.7b (2026-05-07)

- `DANK_LOOKUP_OUTPUT` env var for per-test output path isolation
- Fixture runner uses per-test output files, eliminating shared-file race condition
- ja/yue optional fixture set: `fixtures-ja-yue/` (6 fixtures)

## v0.4.7 (2026-05-07)

- Japanese dictionary deployed: `reader.dict JA` (100,036 words, ja-ja monolingual)
- Cantonese dictionary deployed: `words.hk 粵語詞典` (16,204 entries, converted from CSV via pyglossary)
- Config modes: `ja` (kana + cjk) and `yue` (cjk) added alongside existing `han`/`en-en`
- `convert_wordshk_csv.py` for CSV-to-TSV conversion pipeline

## v0.4.6 (2026-05-07)

- Single-mode pill replaces empty segmented row when only one mode is available
- Copy action copies `resultBody` to clipboard via `wl-copy` (Quickshell.Io.Process)
- Open-full action writes body to `/tmp/dank-lookup/full.txt` and opens with `xdg-open`

## v0.4.5a (2026-05-07)

- Segmented row now uses `availableModes` as Repeater model (was hardcoded `["han","han-en","en-en"]`)
- Segment width adapts to N segments instead of `/3`
- Unconfigured modes (e.g., han-en removed from config) no longer appear as disabled ghosts

## v0.4.5 (2026-05-07)

- Removed hardcoded `modeLabels` (漢語/漢英/英英) from QML; segmented row now reads labels from `results_by_mode[mode].label` from backend JSON
- Enables locale-independent labelling — users set labels via `~/.config/dank-lookup/config.json` per mode
- Tested with en-en + han only experimental config; han-en correctly excluded from segmented row

## v0.4.4b (2026-05-06)

- Backend fixture tests: Linux (exact/related), request (GCIDE+WordNet), 為甚麼 (萌典), consolidate (XDICT)
- 4/4 fixtures passing; restrictive custom config expectedly fails default fixture suite
- Tests at `~/.local/lib/dank-lookup/tests/`

## v0.4.4a (2026-05-06)

- `dank-lookup-selection` forwards `related_by_mode`, `mode_preferences`, `config` metadata
- QML resets all mode state (resultsByMode, relatedByMode, availableModes, activeMode) on every lookup
- Prevents stale tab/body state when switching from full results to mock/no-result

## v0.4.4 (2026-05-06)

- File-based source config at `~/.config/dank-lookup/config.json`
- `DANK_LOOKUP_CONFIG` env var overrides config path
- Missing/invalid config uses built-in defaults
- Python now owns sdcv subprocess calls, config loading, and source routing

## v0.4.3 (2026-05-06)

- Result quality policy: exact entries in `results_by_mode`, fuzzy entries in `related_by_mode`
- Linux → WordNet exact main, GCIDE fuzzy only in related toggle
- QML: related toggle, `activeBody()`, `activeSourceLabel()`

## v0.4.2 (2026-05-06)

- Conservative GCIDE/Webster whitespace cleanup
- Reduced standalone `[1913 Webster]` markers
- Scroll overflow affordance (↓ indicator)
- Scroll position reset on new lookup and mode switch
- en-en exact-match ranking (GCIDE priority, WordNet exact beats GCIDE fuzzy)

## v0.4.1 (2026-05-06)

- Timer 1200ms → 300ms (file polling while popup hidden)
- Script cooldown 3s → 700ms (millisecond precision)
- wl-paste timeout 1s/0.5s → 0.25s
- Body typography: CJK 15px/1.50, Latin 14px/1.42

## v0.4.0 (2026-05-06)

- en-en multi-source (GCIDE + WordNet)
- zh-en / en-zh routing via language classification
- JSON schema: `available_modes`, `results_by_mode`
- monotph dictionary routing

## v0.3.0 (2026-05-06)

- Material 3 card layout: 580x380, outline border, segmented mode row
- Asebi Mincho serif query title (CJK)
- Source label, divider, scroll body (Flickable height 205)
- configurable language via `hasCjkOrKana()` detection

## v0.2.0 (2026-05-06)

- user-scope sdcv backend
- XDICT Traditional Chinese priority
- FreeDict unfiltered fallback
- 萌典重編國語辭典 (han mode)
- 3-tier fallback: primary → secondary → unfiltered → mock

## v0.1.0 (2026-05-06)

- file-based signal (JSON polling)
- DMS floating popup (FloatingWindow)
- Mod+Alt+D keybind
- niri floating window rule
- mock backend
