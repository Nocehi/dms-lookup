# Dank Lookup

Standalone-first, local selection lookup for Wayland.

Select text in any application, press a shortcut, and Dank Lookup opens a fixed
floating card immediately while local StarDict lookup continues asynchronously.
Long dictionary entries remain readable in the scrollable card; an explicit
**Open full** action opens the complete active entry in an external viewer.
Oversized bodies stay out of the normal JSON and are re-fetched locally only
after that action.

The default path needs Quickshell, `wl-clipboard`, Python, and `sdcv`—not Dank
Material Shell. An optional DMS 1.4.6+ daemon wrapper maps DMS theme colors onto
the same shared controller/card.

## Product boundaries

- Local `sdcv`/StarDict lookup; no default network, LLM, OCR, encyclopedia,
  translation, telemetry, or query history.
- The resident UI never monitors or reads the clipboard. The selection command
  reads the primary selection once, only after an explicit invocation. Falling
  back to the regular clipboard is opt-in and never applies to force press.
- Exact and Related results are distinct. All exact modes that return a result
  remain available for manual switching.
- Fresh routing includes `han`, `en`, `ja`, and `yue`; a mode is visibly
  unavailable when its dictionaries are absent.
- Dictionary data is never bundled or downloaded. Unknown/unverified licenses
  are not treated as redistribution permission.
- Local data detectors (date, timestamp, URL, e-mail, unit, colour, number
  base) are pure computation on the query: no fetch, no process, no file.

## Request path

```text
shortcut
  -> dank-lookup-selection
      -> prepare IPC: show loading frame and allocate request id
      -> capturePrepared IPC: UUID/source metadata only
  -> resident standalone Quickshell controller
      -> explicit primary capture (opt-in clipboard fallback) through a child pipe
      -> bounded stdin envelope to `dank-lookup backend-request`
      -> fixed private `backend.sock`; tokenizer/sdcv queries over stdin
      -> apply only matching request-id + sequence
  -> exact/related/mode/token card
```

There is no `latest.json`, 300 ms polling timer, two-second debounce, or
unversioned result slot. A new lookup updates an already-open card. A slow old
completion is ignored.

## Install

```bash
bash packaging/install-user.sh --with-tokenizer   # or --tokenizer-bin PATH / --without-tokenizer
dank-lookup-language-packs status
```

The installer is user-scoped and does not start/reload a process, edit niri, or
overwrite an existing `~/.config/dank-lookup/config.json`. Reinstall and
uninstall touch only files the installer recorded; dictionaries and config are
kept (`--purge-config` removes the config). How installed files are tracked is
described in [`docs/release.md`](docs/release.md#installer-file-ownership).

To keep the frontend resident and avoid a cold process launch:

```bash
systemctl --user daemon-reload
systemctl --user enable --now dank-lookup.service
```

The selection command can also start the named Quickshell config on demand.

Add a compositor shortcut, for example in niri:

```kdl
Mod+Alt+D repeat=false { spawn-sh "$HOME/.local/bin/dank-lookup-selection"; }
```

By default only the primary selection is read; when nothing is selected the
card says so. To also fall back to the regular clipboard, add this top-level
key to `~/.config/dank-lookup/config.json`:

```json
"selection": { "clipboard_fallback": true }
```

Only the literal `true` enables it. An invocation with the source name
`force-press` (`dank-lookup-selection force-press`) never reads the clipboard,
even with the key set, because a deep press over unselected text is easy to
trigger by accident.

and a title-based floating rule:

```kdl
window-rule {
    match title="Dank Lookup"
    open-floating true
}
```

Full niri and optional DMS instructions are in
[`docs/niri-dms-install.md`](docs/niri-dms-install.md).

### Optional DMS wrapper

```bash
bash packaging/install-user.sh --with-dms
dms ipc plugins reload dankLookup
```

The reload command uses the locally verified current syntax; it is not executed
or claimed successful by the installer. The wrapper is a persistent `daemon`,
not a launcher provider. Standalone remains independently usable.

## Dictionaries and fresh modes

The installed version-2 config has four canonical mode ids: `han`, `en`, `ja`,
and `yue`. Legacy user configs with ids such as `en-en` or `han-en` continue to
load unchanged; the UI treats mode ids generically.

```bash
sdcv --list-dicts
dank-lookup-language-packs status --json
```

See [`docs/dictionary-sources.md`](docs/dictionary-sources.md) before installing
or sharing any data. In particular, this project does not bundle words.hk,
XDICT, reader.dict, or any dictionary whose exact artifact/license chain has
not been admitted.

The Rust tokenizer is built and installed as a managed file with
`bash packaging/install-user.sh --with-tokenizer` (needs `cargo`; the
lindera-ipadic build script downloads IPADIC, so the build needs network
access), or from an existing build with `--tokenizer-bin PATH`. It reads the
query on stdin. When no tokenizer is installed, or the installed one is the
v0.7.1 positional-text build, the installer stops before changing anything and
names these options; it never overwrites an unmanaged tokenizer, so move an
old one aside first. `--without-tokenizer` installs on purpose without one:
segmentation then uses a dependency-free, explicitly non-linguistic script/
bigram fallback, capped at 24 tokens and identified in the result metadata; it
does not pretend to be complete Chinese or Japanese language identification.

## UI behavior

- fixed-size card (400x420 by default, configurable) with scrollable
  definitions; each dictionary is a section with a grey source heading, and
  dictionary bold/italic/grey styling is kept (display-only reflow never
  changes copied text). It opens centred, or under the pointer with
  `DANK_LOOKUP_PLACEMENT=pointer` (see `docs/theming.md`);
- explicit source names and mode labels;
- CJK headwords use the `Jigmo` Mincho font when installed (`ttf-jigmo`,
  CC0), otherwise `Asebi Mincho`, which also sets other heading text; set
  `DANK_LOOKUP_HEADWORD_FONT` to override the CJK headword font. Rare Ext B+
  characters need fontconfig fallback to `Jigmo2`/`Jigmo3`; `font.families`
  lists are not available in QML. UI text uses MiSans TC;
- exact/related switch using the active displayed body;
- token row consumes ordered `token_items` and supports duplicate token text;
- query/mode/related/token changes reset scroll predictably;
- 170 ms nonblocking appearance animation; results may update during it;
- Copy sends the active preview to `wl-copy` over stdin;
- when the whole selection is a date, timestamp, URL, e-mail address, unit
  quantity, colour code or `0x`/`0o`/`0b` integer, a **Local detection** chip
  shows a local preview (calendar conversions incl. 民國/和曆/干支/ISO week,
  local time, IDN/punycode and tracking parameters, unit conversion incl.
  regional 斤 and 坪, colour swatch). It is the default view only when no
  dictionary has an exact result; Copy sends the detected value. Disable with
  `"detectors": {"enabled": false}` or per kind under `detectors.kinds`;
- Open full sends a bounded inline exact/related/token body through stdin, or
  sends the query through bounded stdin to re-fetch an oversized body by its
  opaque hash; only that explicit action creates a TTL-cleaned private file,
  then invokes `xdg-open` with argv.

Markup cleanup is conservative. Preview truncation is explicit. Complete
cleaned content is carried once while it remains under the configured inline
cap; an oversized result carries only `full_ref` and `full_chars` in public
JSON. Re-fetch can fail if dictionaries or routing change before Open full.

## Theme contract

Every colour, radius, spacing step, type size, font and elevation value is a
token in `qml/LookupTheme.qml`, shared by standalone and the DMS wrapper; a lint
test rejects literals elsewhere. The palette uses matugen role names:

```text
primary, on_primary, surface, surface_container_high,
on_surface, outline, error, on_error (+ mode/dark)
```

Built-in light/dark palettes work without generated files. The installer ships
complete matugen 4.x JSON templates; `DANK_LOOKUP_THEME_FILE` enables safe
last-known-good reload. DMS Theme values are mapped onto token names only
inside the optional wrapper. See [`docs/theming.md`](docs/theming.md) for the
token layout, the DMS 2.0 baseline and the `~/.config/matugen/config.toml`
section to add.

## Tests

Dank Lookup supports Python 3.11 and newer. How to run the test suite, the
benchmarks, the giant-entry RSS gate and the real-Wayland checks, and how a
release evidence bundle is built, is in [`docs/release.md`](docs/release.md).

## Known limitations

- Wayland gives no access to arbitrary on-screen text: select text first.
  Primary-selection behaviour differs between applications.
- The opt-in clipboard fallback may return stale clipboard content when
  nothing is selected.
- niri with optional DMS is the tested target; other compositors are untested.
- Result quality depends on the installed dictionaries; some (for example the
  XDICT 漢英 data) are poor and cannot be fixed here.
- Entry text is held as a Python string, which CPython stores at the width
  of its widest character: an entry that is mostly ASCII but contains one IPA
  character takes about 2x its UTF-8 file size in memory, and one emoji or
  CJK extension character about 4x. Giant entries are therefore larger in
  memory than their dictionary size suggests. Pure CJK is the worst case
  for the giant-entry gate: its string is smaller than its UTF-8 size, so a
  UTF-8 copy alive at the same time (for example sdcv's JSON output before
  it is parsed, which the benchmark does not cover) weighs about 1.5x more.
- The force-press trigger is experimental and specific to the Apple Magic
  Trackpad (A3120); it needs a udev rule installed with root.

## Privacy

Private runtime state uses `$XDG_RUNTIME_DIR/dank-lookup` with directory mode
`0700`, file/socket mode `0600`, `umask 077`, atomic replacement, and a verified
`/tmp/dank-lookup-$UID` fallback. Normal lookups retain no result file. Files
created by Open full have automatic short-TTL cleanup (300 seconds by default).
See
[`docs/privacy.md`](docs/privacy.md) for lifecycle and threat boundaries.

## Repository layout

```text
bin/                 selection and standalone CLI
lib/                 importable core, secure runtime, backend adapters
qml/                 DMS-free shared controller/card/theme
standalone/          Quickshell config entry point
plugin/              optional DMS daemon wrapper
language-packs/      no-download manifest, default routing, status tool
systemd/             optional resident user service
tests/               unit, fixture, tokenizer, and latency harnesses
packaging/           guarded user installer/uninstaller/release checks
docs/                contract, privacy, source, and compositor documentation
```

## License

MIT for project code. Dictionary data is not included and has separate terms.
