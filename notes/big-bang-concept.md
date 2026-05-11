---
title: "Dank Lookup Big Bang tokenizer: concept"
status: "concept"
target: "opencode"
scope: "segmentation fallback for multi-word CJK selections"
created: "2026-05-07"
inspiration: "Smartisan Big Bang"
---

# Dank Lookup Big Bang tokenizer: concept

## What is Big Bang

Smartisan OS's Big Bang feature: long-press on a block of Chinese text, and it "explodes" into individual words. Each word becomes a tappable chip — tap to search, translate, or share.

The name comes from the visual effect: a paragraph "explodes" into tokens, like particles scattering from a central point.

## Relevance to Dank Lookup

Currently `dankLookup` receives selected text as one string. The backend looks up the entire string. If the string is a phrase or sentence, no dictionary result is found.

Example:
- User selects "為甚麼會這樣"
- sdcv finds no result (no dictionary has this as a single entry)
- Returns mock fallback

Big Bang would segment the selected text and look up each token independently:

```
"為甚麼會這樣"
    │
    ▼  segmentation
["為甚麼", "會", "這樣"]
    │
    ▼  per-token lookup
{
  "為甚麼": { han: 萌典 (為甚麼: 詢問原因), han-en: XDICT (why) },
  "這樣":   { han: 萌典 (這樣: 如此), han-en: XDICT (like this) }
}
```

## When to trigger

Big Bang is NOT enabled by default. It activates only as a fallback:

1. User selects text → `Mod+Alt+D`.
2. Backend tries exact lookup (current behavior).
3. If ALL enabled modes return no result AND selected text has multiple CJK characters:
   → trigger segmentation fallback
   → display per-token results in a token pill row above the definition body

## Segmentation engine

Candidate: **jieba** (结巴分词) — pure Python, no network, MIT license.

```python
import jieba
tokens = list(jieba.cut("為甚麼會這樣"))
# → ["為甚麼", "會", "這樣"]
```

Installed via `pip install jieba` (user-scope, mit) or bundled as a vendored module in a later release.

License confirmed 2026-05-07: jieba upstream LICENSE is MIT, compatible with dankLookup's MIT.

### Ambiguity handling

- "不要用壞了" → jieba default: ["不要", "用", "壞", "了"]
- For V1, accept default segmentation.
- Mis-segmentation mitigation: user can always manually select a shorter substring before lookup.

## Backend contract

Backend adds a new top-level key when segmentation is active:

```json
{
  "backend": "sdcv",
  "segmented": true,
  "tokens": ["為甚麼", "會", "這樣"],
  "results_by_token": {
    "為甚麼": {
      "default_mode": "han",
      "available_modes": ["han"],
      "results_by_mode": { }
    },
    "這樣": {
      "default_mode": "han",
      "available_modes": ["han"],
      "results_by_mode": { }
    }
  }
}
```

Existing `results_by_mode` stays for backward-compatible exact lookup.

## QML display

Token row replaces the segmented row when `segmented === true`:

```
┌───────────────────────────────┐
│  ✕  為甚麼會這樣              │
│  [為甚麼] [會] [這樣]          │  ← token pills, click to select
│                               │
│  Token: 這樣                  │
│  萌典重編國語辭典              │
│ ──────────────────────────── │
│  這樣: 如此、這般             │
│  這般 / 如此                  │
└───────────────────────────────┘
```

Clicking a token pill shows the definition for that token, with its own mode tabs.

## Implementation phases

### Phase 1: backend only (v0.5.x)

- jieba integration in `sdcv.sh` (Python block)
- Segmentation fallback when all modes empty
- Per-token `results_by_token` JSON
- No QML changes yet

### Phase 2: QML token row (v0.6.x)

- Replace segmented row with token pills when `segmented === true`
- Token tap → show definition for that token
- Back button or "All tokens" toggle

### Phase 3: ergonomics (v0.7.x)

- Long-press on token to re-segment differently
- Manual re-selection hint when segmentation is clearly wrong
- Config-driven segmentation granularity preference

## Acceptance for concept doc

- Exact lookup remains the first path.
- Segmentation fallback is opt-in or dependency-gated.
- No OCR.
- No network request.
- No LLM.
- No assumption that segmentation is always correct.

## Next dictionary planning note

Japanese and Cantonese should prefer monolingual dictionaries where practical.

| Language need | Preferred direction | Notes |
|---------------|---------------------|-------|
| Japanese | ja-ja source first, ja-en fallback later | JMdict / EDICT is useful but mostly bilingual |
| Cantonese | yue-yue source first, yue-en/zh-en fallback later | CC-CEDICT is not Cantonese monolingual; words.hk is a strong candidate |
| Mandarin / Traditional Chinese | 萌典 remains the strong monolingual source | Already integrated locally |
| English | WordNet / GCIDE remain monolingual sources | Already integrated locally |

### License caution

- Do not bundle dictionary data in a future GitHub repo unless the license is explicit and compatible.
- For each source, record: upstream URL, license, redistribution permission, conversion source, whether monolingual or bilingual.
- words.hk CSV is "ALL RIGHTS RESERVED. DO NOT DISTRIBUTE" — may be used under https://words.hk/base/hoifong/ (need to verify specific terms).
