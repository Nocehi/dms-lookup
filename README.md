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
  reads it once, only after an explicit invocation.
- Exact and Related results are distinct. All exact modes that return a result
  remain available for manual switching.
- Fresh routing includes `han`, `en`, `ja`, and `yue`; a mode is visibly
  unavailable when its dictionaries are absent.
- Dictionary data is never bundled or downloaded. Unknown/unverified licenses
  are not treated as redistribution permission.

## Request path

```text
shortcut
  -> dank-lookup-selection
      -> prepare IPC: show loading frame and allocate request id
      -> capturePrepared IPC: UUID/source metadata only
  -> resident standalone Quickshell controller
      -> explicit primary capture (clipboard fallback) through a child pipe
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
bash packaging/install-user.sh
dank-lookup-language-packs status
```

The installer is user-scoped and does not start/reload a process, edit niri, or
overwrite an existing `~/.config/dank-lookup/config.json`. A new routing config
is mode `0600`; reinstall preserves it, and uninstall keeps it unless
`--purge-config` is explicit. Each installed regular file is recorded by root
key and normalized relative path in the mode-0600
`~/.local/lib/dank-lookup/managed-files.json`. Upgrade removes only old-minus-new
entries after new files are installed; omitting `--with-dms` preserves an
existing managed DMS scope. Marker-only old installs use an explicit legacy
path list. Uninstall removes only validated entries and empty managed
directories, preserving dictionaries, config, and unlisted files.
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

The optional Rust tokenizer remains supported when separately installed. A
fresh install also has a dependency-free, explicitly non-linguistic script/
bigram fallback, capped at 24 tokens and identified in the result metadata; it
does not pretend to be complete Chinese or Japanese language identification.

## UI behavior

- fixed/min/max popup with scrollable plain-text definitions;
- explicit source names and mode labels;
- exact/related switch using the active displayed body;
- token row consumes ordered `token_items` and supports duplicate token text;
- query/mode/related/token changes reset scroll predictably;
- 170 ms nonblocking appearance animation; results may update during it;
- Copy sends the active preview to `wl-copy` over stdin;
- Open full sends a bounded inline exact/related/token body through stdin, or
  sends the query through bounded stdin to re-fetch an oversized body by its
  opaque hash; only that explicit action creates a TTL-cleaned private file,
  then invokes `xdg-open` with argv.

Markup cleanup is conservative. Preview truncation is explicit. Complete
cleaned content is carried once while it remains under the configured inline
cap; an oversized result carries only `full_ref` and `full_chars` in public
JSON. Re-fetch can fail if dictionaries or routing change before Open full.

## Theme contract

Standalone uses six colors plus a dark flag:

```text
primary, on_primary, surface, surface_container_high,
on_surface, outline, dark
```

Built-in light/dark palettes work without generated files. The installer ships
generic matugen 4.x JSON templates; `DANK_LOOKUP_THEME_FILE` enables safe
last-known-good reload. DMS tokens are mapped only inside the optional wrapper.

## Tests and benchmarks

```bash
python3 -m unittest -v tests.test_core tests.test_runtime tests.test_cli
bash packaging/pre-release-check.sh
python3 tests/run_qml_smoke.py
qmllint /path/to/staged/quickshell/dank-lookup/shell.qml
python3 tests/benchmark_lookup.py \
  --config "$HOME/.config/dank-lookup/config.json" \
  --cold-samples 5 --warm-samples 30 \
  --output /tmp/dank-lookup-benchmark.json
python3 tests/benchmark_ui.py \
  --config "$HOME/.config/dank-lookup/config.json" \
  --cold-samples 3 --warm-samples 10 \
  --output /tmp/dank-lookup-ui-benchmark.json
python3 tests/recalculate_benchmark_percentiles.py \
  /tmp/dank-lookup-benchmark.json /tmp/dank-lookup-ui-benchmark.json \
  --output /tmp/dank-lookup-percentiles-recalculated.json
```

The backend benchmark emits JSON plus a human p50/p95/p99 summary and labels its
scope honestly: it compares one-shot, in-process resident, and private
idle-exit-socket strategies but does not call backend time a UI frame metric.
The UI harness installs into a temporary HOME, uses synthetic explicit IPC
instead of reading a selection, and reports only
`first_qml_frame_tick_ms`/`result_qml_frame_tick_ms` proxy distributions. Its
JSON marks `offscreen=true` and `proxy=true`, retains every raw sample/failure,
and derives p50/p95/p99 from those arrays. It is not visible/presented-frame or
selection-to-visible timing. `tests/verify_wayland_presentation.py` separately
records real-Wayland owner pass/fail; without compositor timestamp evidence it
emits no presentation latency. Default runtime writes no telemetry. Offscreen
Quickshell IPC may need to run outside a filesystem/socket sandbox.

`packaging/build-review-bundle.sh --output PATH` reads the privacy sentinel only
from stdin. It refuses incomplete unit/integration/pre-release/QML, selection,
backend/QML benchmark, giant-RSS, real-Wayland/DMS, privacy, command,
environment, or percentile-recalculation evidence. Source candidates come from
Git's tracked and untracked lists, deleted/non-regular candidates are filtered,
and the final archive is checked bidirectionally against staging
`included-files.txt`, SHA-256 checksums, and the sentinel scan. Missing
historical evidence is never backfilled.

Fixture sets now require an explicit config/backend; their results are reported
per set, never collapsed into a generic historical “all passed”. See
[`docs/json-schema.md`](docs/json-schema.md) for the current result/IPC contract.

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
