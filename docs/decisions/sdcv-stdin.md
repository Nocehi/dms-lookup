# Decision: send sdcv queries over stdin

- Decided: 2026-07, re-confirmed 2026-11-07 for 0.8.0 (commit 9f3b6ca).
- Tool version the decision was made on: **sdcv 0.5.5**.
- Probe: `python3 tests/probe_sdcv_stdin.py` (writes
  `tests/evidence/sdcv-stdin/gate-<UTC>/report.json` and raw stdout/stderr).
- Result on 2026-11-07 (`gate-20261107T193603Z`): verdict pass, no gate
  errors, all 18 cases pass: exact hit/miss, fuzzy choice, single/multiple
  dictionaries, en/han/ja/yue Unicode, stdin EOF after an exact query and at
  the main prompt, source and overall timeouts, cancellation, 32 MiB stdout
  under the cap, stdout and stderr over the cap, and the query absent from
  `/proc/<pid>/cmdline`.

The product passes each query to sdcv on stdin
(`lib/dank_lookup_sdcv_stdin.py`), so the query never appears in a process
argument list. This is a one-time decision, not per-release evidence.

## When to re-run

`packaging/review-evidence-requirements.json` records the version under
`decisions.sdcv_stdin.version`. The review bundle compares it with
`sdcv --version` on the machine that builds the bundle and refuses to build
when they differ. Then run the probe, check that every case still passes,
update the result and version here and in the requirements file, and commit.
