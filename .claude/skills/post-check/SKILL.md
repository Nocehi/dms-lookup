---
name: post-check
description: >-
  Use after any code change to dank-lookup. Syncs documentation,
  runs fixture suites, and checks release readiness.
---
## After code changes

### Documentation sync
- Update CHANGELOG.md with new version entry
- Update ~/文件/DankLookup/github-release-roadmap.md
- Check for stale status markers:
  grep -rn "status: \"planned\"" ~/文件/DankLookup/*.md | grep -v session

### Fixture suites
Run all five suites. Do not summarise as all-pass without running each:

DANK_LOOKUP_CONFIG=/tmp/dank-lookup-no-such-config.json \
  python3 ~/.local/lib/dank-lookup/tests/run_backend_fixtures.py \
  ~/.local/lib/dank-lookup/tests/fixtures

DANK_LOOKUP_CONFIG=~/.local/lib/dank-lookup/tests/configs/ci-minimal.json \
  python3 ~/.local/lib/dank-lookup/tests/run_backend_fixtures.py \
  ~/.local/lib/dank-lookup/tests/fixtures-ci-minimal

python3 ~/.local/lib/dank-lookup/tests/run_backend_fixtures.py \
  ~/.local/lib/dank-lookup/tests/fixtures-ja-yue

python3 ~/.local/lib/dank-lookup/tests/run_backend_fixtures.py \
  ~/.local/lib/dank-lookup/tests/fixtures-reading-routing

python3 ~/.local/lib/dank-lookup/tests/run_backend_fixtures.py \
  ~/.local/lib/dank-lookup/tests/fixtures-risk-hardening

### Release readiness
bash ~/dank-lookup/packaging/pre-release-check.sh
