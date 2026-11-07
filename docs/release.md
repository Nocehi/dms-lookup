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
before running the minimum suite; missing, version-skewed, or active
marker-selected dependencies are rejected by the pre-release preflight.

```bash
python3 -m venv /tmp/dank-lookup-venv
. /tmp/dank-lookup-venv/bin/activate   # fish: source /tmp/dank-lookup-venv/bin/activate.fish
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

## Release evidence

Release evidence is bound to one Git commit. The commands that produce
automated evidence are fixed in
[`packaging/review-evidence-requirements.json`](../packaging/review-evidence-requirements.json)
and are read from the release commit itself, so a caller cannot substitute
another command and an uncommitted edit to the file has no effect.

```bash
# 1. Start a context for the commit to release (default HEAD).
python3 packaging/release_evidence.py start --context tests/evidence/release/context-SHORT.json

# 2. Automated evidence: every group runs in a temporary `git worktree` of
#    that commit, so untracked and uncommitted files do not exist there.
#    Each group writes tests/evidence/release/SESSION/GROUP.log (command,
#    output, exit code) plus its declared outputs, and is recorded only when
#    every command exits 0. --config is the dictionary config the
#    benchmarks read (default ~/.config/dank-lookup/config.json).
python3 packaging/release_evidence.py run --context tests/evidence/release/context-SHORT.json --all

# 3. Owner-run evidence (selection latency, real Wayland/DMS sessions).
python3 packaging/release_evidence.py record --context tests/evidence/release/context-SHORT.json \
  --group wayland_owner --path tests/evidence/release/wayland/niri-standalone.json

# 4. Build the bundle; the output must be outside the repository.
bash packaging/build-review-bundle.sh --release-context tests/evidence/release/context-SHORT.json \
  --output ../dank-lookup-review-VERSION-SHORT.tar.gz
```

Rules:

- Automated evidence is re-run for every commit; it is never carried over.
- Owner-run evidence names the code it measured in `tested_source`
  (commit and whether it had uncommitted changes). `verify_wayland_presentation.py`
  reads it from `~/.local/lib/dank-lookup/installed-source.json`, which
  `install-user.sh` writes from the checkout it installs (untracked QML counts
  as a change, because the installer copies it). The selection latency
  scripts read it from the checkout they run from. `record` refuses a file
  without it, a file that measured uncommitted code, and a file whose commit
  differs from the release commit in a path the group depends on; the bundle
  checks the same again. So re-install before taking real-session evidence.
- What a manual group depends on: the `depends_on` globs it declares in the
  requirements file, or, when it declares none, every path that is not
  documentation (`*.md`, `docs/`). The selection latency groups depend only on
  their measuring scripts, because they time `wl-paste` and never run
  `bin/` or `lib/`. The Wayland and DMS groups declare nothing.
- `release_evidence.py adopt --context NEW --from OLD` carries owner-run
  evidence into a new context under the same rule.
  Contexts from 0.8.0 and earlier (schema 1) are not bound to a commit, so
  their owner-run evidence cannot be adopted; take it again.
- Logs of groups that read the dictionary config (`{config}`) record its path
  and SHA-256, so a later run on another config is visible. The dictionaries
  themselves are not hashed.
- Missing evidence is never backfilled: evidence from an older commit does
  not count, apart from the adoption rule above.
- The bundle holds `source/` (`git archive` of the commit), `evidence/`
  (every recorded file), `environment.json` (captured when the bundle is
  built), `binding.json` (commit, tree hash, every evidence checksum) and
  `checksums.sha256` for everything outside `source/`. Verify `source/`
  against the tree hash in the binding.
- Before building, the bundle checks the product verdicts: QML smoke,
  privacy transport and pre-release logs pass; benchmarks carry schema-2 raw
  samples and the percentile recalculation matches; the giant-entry RSS report
  passes for every text kind and size; the selection dataset has at least
  three owner classes with 100 present and 20 absent/exit samples; real
  Wayland and DMS records pass.

One-time architecture decisions live in [`docs/decisions/`](decisions/), not
in per-release evidence. The sdcv stdin decision records the sdcv version it
was made on; the bundle refuses to build when the installed `sdcv --version`
differs, until the probe is re-run and the record updated.

Fixture sets now require an explicit config/backend; their results are reported
per set, never collapsed into a generic historical “all passed”. See
[`docs/json-schema.md`](docs/json-schema.md) for the current result/IPC contract.
