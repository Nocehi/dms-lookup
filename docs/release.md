# Release checks and evidence

This page is for whoever prepares or reviews a release. Product usage and
installation are in the [README](../README.md).

## Installer file ownership

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

## Tests and benchmarks

Dank Lookup supports Python 3.11 and newer. On a fresh checkout, create an
isolated Python environment and install the exact release/test dependency set
before running the minimum suite. The same `jsonschema` installation is used
by the production review-bundle consumer; missing, version-skewed, or active
marker-selected dependencies are rejected by the pre-release preflight.

```bash
python3 -m venv /tmp/dank-lookup-venv
. /tmp/dank-lookup-venv/bin/activate
python3 -m pip install --requirement requirements-dev.txt
```

The pre-release shell gate additionally requires Bash, ripgrep, ShellCheck,
GNU coreutils/findutils, and grep on `PATH`. Rust tokenizer tests require a
Rust toolchain; QML smoke requires the locally supported Quickshell/QML tools.

```bash
python3 -m unittest -v \
  tests.test_benchmarks \
  tests.test_cancellation_full_stack \
  tests.test_cli \
  tests.test_core \
  tests.test_dependency_preflight \
  tests.test_design_tokens \
  tests.test_fixture_runner \
  tests.test_packaging \
  tests.test_runtime \
  tests.test_selection_latency \
  tests.test_transport
bash packaging/pre-release-check.sh
cargo test --manifest-path tokenizer/Cargo.toml --locked
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
selection-to-visible timing. Known gap: the socket strategy in
`benchmark_lookup.py` uses an in-process client, so it does not include the
`dank-lookup backend-request` process the card starts for every lookup (about
70 ms on the reference machine before the lookup core was imported lazily);
compare later architectures against a measurement of that real path.
`tests/benchmark_giant_entry_memory.py` measures 1/8/32 MiB entries (sizes in
UTF-8 bytes) of four kinds: ASCII, pure CJK, ASCII with one IPA character and
ASCII with one astral character. Since 2026-11-07 the RSS gate compares the
32-minus-1 MiB RSS growth with the growth of the entry's CPython string size
(`str_ratio`); the earlier basis, UTF-8 input size (`utf8_ratio`), is still
recorded for every kind but not judged, and the report states both
definitions. `tests/verify_wayland_presentation.py` separately
records real-Wayland owner pass/fail; without compositor timestamp evidence it
emits no presentation latency. Default runtime writes no telemetry. Offscreen
Quickshell IPC may need to run outside a filesystem/socket sandbox.

Start a release context before collecting evidence, then record each generated
evidence file with `packaging/release_evidence.py`. Finally run
`packaging/build-review-bundle.sh --release-context CONTEXT --output PATH`.
`PATH` must resolve outside the repository and must not overlap the release
context, requirements, binding, recorded evidence, or any source input; this
collision check runs before the builder creates temporary or durable output.
The context binds one release session and every selected evidence checksum to
source-manifest schema 2, algorithm
`sha256-regular-path-mode-size-and-bytes-v2`. Each entry and the overall digest
bind regular-file type, path, `stat.S_IMODE` permission bits, size, and bytes;
setuid/setgid or non-regular source candidates are rejected. Generated
evidence, caches, archives, and the external audit handoff are excluded from
that source digest and from source staging.

The bundle command generates one privacy sentinel in orchestration-process
memory, passes that same value to the privacy verifier and internal bundle
builder through separate protected anonymous file descriptors, and never puts
it in arguments, environment variables, logs, or release metadata. It refuses
incomplete unit/integration/pre-release/QML, selection, backend/QML benchmark,
giant-RSS, real-Wayland/DMS, privacy, command, environment, or percentile
evidence. The final check reopens both tar and compressed archives and scans
member names, metadata, and uncompressed contents, in addition to enforcing
bidirectional `included-files.txt` and SHA-256 checksum parity. Missing
historical evidence is never backfilled.

Fixture sets now require an explicit config/backend; their results are reported
per set, never collapsed into a generic historical “all passed”. See
[`docs/json-schema.md`](docs/json-schema.md) for the current result/IPC contract.
