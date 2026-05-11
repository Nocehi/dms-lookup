---
title: Per-app context
status: concept
scope: config system, niri integration
created: 2026-05-11
origin: fusuma config/searcher.rb + fusuma-plugin-appmatcher analysis
---

Pattern observed in fusuma; not a v1.0 requirement, kept here for reference if scale grows.

## Why this exists in fusuma

Fusuma v2 uses YAML document separator (`---`) to split config into
per-application blocks. `Searcher.search_with_context` resolves in order:
no-context block -> complete context match -> partial context match.

```yaml
swipe:
  3:
    command: default-thing
---
context:
  frontend: Firefox
swipe:
  3:
    command: firefox-specific-thing
```

`fusuma-plugin-appmatcher` provides the `frontend` context key by querying
the active window via D-Bus / `xdotool`.

## Why DankLookup doesn't need this yet

There is only one trigger (`Mod+Alt+D` or future 4-finger tap). Mode
selection is based on query language classification, not app context.
The current flow works for all apps uniformly.

## When this becomes relevant

- "In Obsidian, default to han-en for bilingual notes; in Bilibili, default
  to ja for Japanese-subtitled content" -- real scenario, but not yet
  validated by daily use.
- Requires `niri msg windows --json` to get focused window `app_id`.
  Adds ~20ms latency + failure surface (niri IPC unavailable).

## Sketch (not a spec)

JSON doesn't have `---`, so use array of objects:

```json
{
  "version": 2,
  "modes": { "..." : "..." },
  "contexts": [
    {
      "match": { "app_id": "org.mozilla.firefox" },
      "default_for_override": { "cjk": "ja", "latin": "en-en" }
    },
    {
      "match": { "app_id": "md.obsidian.Obsidian" },
      "default_for_override": { "latin": "han-en", "cjk": "han" }
    }
  ]
}
```

Lookup order:
1. Context match found -> use that context's `default_for_override`
2. No match -> use mode definitions' `default_for` (current behavior)

## Technical notes

- `niri msg windows --json` returns `app_id` field for the focused window.
- Must handle: niri IPC unavailable, unknown app_id, multiple monitors.
- Should only query app_id once per invocation, before backend call.
- Fallback to no-context behavior on any IPC failure -- never block lookup.
