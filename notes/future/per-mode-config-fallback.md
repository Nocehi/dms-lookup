---
title: Per-mode config fallback
status: concept
scope: config system
created: 2026-05-11
origin: fusuma config.rb / config/searcher.rb analysis
---

Pattern observed in fusuma; not a v1.0 requirement, kept here for reference if scale grows.

## Why this exists in fusuma

Fusuma manages ~40 gesture combinations (swipe/pinch/hold/tap x 1-4 fingers x
directions). A flat config would be unreadable, so it uses a 3-layer lookup:

```
gesture-specific value  >  global root value  >  code default
```

Implemented via `Config#fetch_config_params` calling `Searcher.find_context`
with fallback chain `[:no_context, :plugin_default_context]`.

Example from `tap_detector.rb`:

```ruby
config_value = Config.search(keys_specific) ||   # tap.1.interval
               Config.search(keys_global) ||      # interval.tap
               1                                   # hardcoded default
```

## Why DankLookup doesn't need this yet

DankLookup has 5 modes x 1 trigger. The current flat `config.json` with
per-mode `sources` / `show_related` / `default_for` is sufficient. There is
no combinatorial explosion that demands hierarchical fallback.

## When this becomes relevant

- If per-mode behavioral parameters proliferate (debounce, ranking weights,
  max_entries, strip_html flags) and most modes share the same defaults.
- If community contributors add modes and need sensible defaults without
  copying boilerplate.

## Sketch (not a spec)

```json
{
  "version": 2,
  "defaults": {
    "debounce_ms": 200,
    "max_entries": 20,
    "strip_html": true
  },
  "modes": {
    "ja": {
      "label": "日語",
      "debounce_ms": 50,
      "sources": ["..."]
    },
    "han": {
      "label": "漢語",
      "sources": ["..."]
    }
  }
}
```

Lookup: `ja.debounce_ms` -> 50 (mode-specific). `han.debounce_ms` -> missing
-> `defaults.debounce_ms` -> 200. Implementation is a single `get_param()`
helper in the Python backend.

Config version bump from 1 to 2; v1 configs remain valid (no `defaults` key
-> code defaults apply).
