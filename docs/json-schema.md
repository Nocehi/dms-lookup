# Lookup and IPC contract

The standalone path does not communicate through `latest.json` or a polled
shared slot. Quickshell receives metadata-only IPC, shows the loading frame,
captures the explicitly requested selection through a child pipe, and writes a
bounded request envelope to the fixed private backend transport. The client
emits one JSON document on stdout. Schema version 2 is the current contract.

## Request lifecycle

The standalone IPC target is `dank-lookup`; the optional DMS target is
`dank-lookup-dms`.

| Function | Purpose |
|---|---|
| `prepare(source) -> request_id` | Increment sequence, show/reset the loading card immediately, and return correlation id before clipboard capture. |
| `capturePrepared(request_id, source)` | Start explicit selection capture for the active prepared request. A stale id is rejected; no query enters IPC argv. |
| `changeMode(mode)` | Re-run the in-memory query with a `mode_change` envelope. |
| `changeSource(source)` | Re-run the in-memory query with a `source_change` envelope. |
| `relookup(source, mode)` | Re-run the in-memory query with a `relookup` envelope. |
| `status()` | Return phase, sequence, and visible state; no query text. |
| `benchmarkState()` | In opt-in benchmark mode, return numeric monotonic spans; no query/result content. |

Every process completion carries both `request_id` and `sequence`. The frontend
applies it only when both match the current request, so a slower old request
cannot replace a newer result. A popup-open request updates the same window.

## Backend CLI

```bash
printf '%s' 'QUERY' | dank-lookup lookup \
  --request-id REQUEST_ID \
  --sequence SEQUENCE \
  --config /optional/config.json
```

`lookup` accepts bounded UTF-8 stdin only and uses the same fixed private socket
as the QML client. `backend-request` likewise accepts only a bounded JSON stdin
envelope. Normal lookup modes retain no query/result file. `--retain-runtime`
is an explicit compatibility/test option. `lib/backends/sdcv.sh` requires
`--stdin` and can write a specified `DANK_LOOKUP_OUTPUT` atomically.

Open-full helpers are deliberately separate from lookup:

```bash
# Inline complete body on UTF-8 stdin.
dank-lookup open-text --request-id REQUEST_ID

# Oversized result: query on bounded UTF-8 stdin, ref/config in argv.
dank-lookup open-ref --request-id REQUEST_ID --ref OPAQUE_HASH \
  --config /optional/config.json
```

`open-ref` re-runs the local lookup in memory and opens only the body whose
content hash matches the active result. It can fail if the routing config or
dictionaries change between lookup and the explicit Open action.

## Source lookup policy

Every source should declare a boolean `exact_only`. When `true`, only an NFKC
casefold-equal headword is accepted. When `false`, bounded non-exact candidates
from the adapter are retained under `related_by_mode`. The policy decides how
much the adapter fetches: an exact-only source fetches only fuzzy candidates
whose headword is NFKC-casefold equal to the query (sdcv's exact search is
case-sensitive), and skips the fuzzy search entirely for a query with no such
variants, such as CJK text.
For old configs that omit `exact_only`, `allow_related` is read only as a legacy
fallback; fresh configs explicitly set `exact_only`.

## Result object

| Field | Type | Description |
|---|---|---|
| `schema_version` | integer | `2`. |
| `backend` | string | `sdcv` or `unavailable`. |
| `query`, `title` | string | Normalized explicit query. Never logged by the frontend. |
| `query_truncated` | boolean | True only when an over-4096-character direct request was rejected; no backend call is made. |
| `request_id` | string | Correlation id. |
| `sequence` | integer | Ordering value. |
| `features` | object | Order-independent script flags/classes (`han`, `kana`, `latin`, `mixed`, `cantonese_marker`, `unknown`). |
| `candidate_modes` | string[] | Legal configured modes for the detected features, before result filtering. |
| `installed_modes` | string[] | Modes with at least one detected local source. |
| `unavailable_modes` | string[] | Configured modes with no detected local source. |
| `incomplete_modes` | string[] | Candidate modes whose sources were cut short by the lookup budget or a source timeout; results for them may be missing. For a segmented lookup it also includes every mode cut short in any token. |
| `incomplete_tokens` | string[] | `token_items[].key` of tokens with a non-empty `incomplete_modes`; empty unless `segmented`. The card names these tokens as timed out. |
| `available_modes` | string[] | Candidate modes that returned an exact result. All are retained for manual switching. |
| `default_mode` | string or null | Ranked preference among available modes; Kana and strong Cantonese markers are boosts, not exclusion rules. |
| `results_by_mode` | object | Exact results keyed by mode id. |
| `related_by_mode` | object | Non-exact results keyed by mode id when the source permits related lookup. |
| `mode_preferences` | object | UI hints such as `show_related`; not an availability claim. |
| `mode_status` | object | Configured/detected source details per mode. |
| `segmented` | boolean | Whether phrase token lookup produced usable token results. |
| `segmentation` | object | `{engine,warning}`; identifies the external tokenizer or honest non-linguistic fallback. |
| `tokens` | string[] | Ordered display tokens. Duplicate text is allowed. |
| `token_items` | object[] | Ordered `{key,index,token}` records. `key` disambiguates duplicate tokens. |
| `results_by_token` | object | Token result objects keyed by `token_items[].key` (currently `index:token`); readers may accept legacy raw-token keys. |
| `subtitle`, `body`, `truncated` | mixed | Compatibility projection of the default exact mode's preview. |
| `config` | object | Attempted path, loaded flag, and non-sensitive warning code. |
| `warnings` | string[] | Backend warning codes; partial success may retain warnings with `error: null`. No query content. |
| `error` | object or null | Structured `{code,message}` failure. Codes include `query_too_long`, `empty_query`, `sdcv_missing`, `lookup_timeout`, and `backend_error`. Structured lookup failures are JSON results and may use process exit 0. |
| `timing` | object | Monotonic backend start plus measured backend/total milliseconds. |
| `runtime` | object | CLI-added retention state; `retained` is false by default. |

## Mode result

Each exact/related mode result contains:

| Field | Type | Description |
|---|---|---|
| `label` | string | User-facing mode or Related label. |
| `subtitle` | string | Exact dictionary source names. |
| `body` | string | Bounded, plain-text preview. |
| `full_body` | string or null | Complete conservatively cleaned content when it fits the inline cap; null for oversized content. |
| `full_ref` | string or null | Opaque content hash for an oversized complete body; null while `full_body` is inline. |
| `full_chars` | integer | Character count of the complete cleaned body, whether inline or referenced. |
| `truncated` | boolean | True when preview omits content; the UI says full content is available. |
| `entries` | object[] | Entry metadata/preview records. Complete text is not duplicated in each record. |
| `entry_count` | integer | All admitted exact/related entries. |
| `preview_entry_count` | integer | Entries represented in the bounded preview. |

An entry record includes `entry_id`, `raw_ref`, `word`, `source`, bounded `body`,
and `truncated`, and optionally `runs`: display styling for `body` as
`[text, flags]` pairs whose texts concatenate to exactly `body`. Flags are a
string of `b` (bold), `i` (italic) and `d` (dimmed/secondary), empty for plain
text. Styling comes from the bold, italic and coloured spans of HTML sources and
from conservative line rules (sense numbers, bracketed readings and
part-of-speech tags, example and citation lines, sense separators). It never
changes text; `runs` is omitted when nothing is styled, and consumers that
ignore it lose nothing. The private vault used by opt-in retention stores one canonical
cleaned body per retained entry; it does not duplicate the unmodified backend
definition. For an inline result, **Open full** sends the
active exact/related/token `full_body` over stdin to `open-text`. For an
oversized result, QML sends the active query over stdin to `open-ref`; the
helper re-runs the lookup, verifies `full_ref`, and only then materializes the
complete private file. Public lookup JSON therefore stays bounded.

Dictionary markup is parsed only when an explicit source override or an exact
`.ifo` `bookname` match with `sametypesequence=h` classifies it as HTML.
`sametypesequence=m`, conflicts, mixed/unknown sequences, or missing metadata
use plain cleanup and retain literal angle-bracket prose. Flattened field
markers cannot be reconstructed when `.ifo` format metadata is absent; content
is not guessed as HTML. Preview limits are explicit config fields; the full
body is not silently replaced by preview.

## Segmentation result

Each `results_by_token[key]` object contains `key`'s `token`/`index` plus the
same candidate/availability/default/exact/related fields as a top-level lookup.
The frontend uses `token_items`, so repeated tokens select distinct result
objects and reset scroll/mode/related state predictably.

Segmentation runs only when the whole query has no top-level exact mode. An
optional external tokenizer is bounded by the remaining global lookup budget
and at most two seconds. Without it, fresh installs use an explicitly
non-linguistic script-run/bigram fallback reported as
`fallback-script-ngram` with `fallback_segmentation_nonlinguistic`. If an
installed tokenizer predates the stdin CLI (it rejects `--stdin`), the same
fallback is reported with `tokenizer_incompatible` instead. At most 24
tokens are queried. `segmented` becomes true only when at least one token has a
usable exact mode; empty token records remain unselectable in the UI.

The default safety budgets are 1,500 ms per local `sdcv` process and 3,000 ms
for a lookup as a whole. `source_timeout_ms` is clamped to 100–5,000 ms and
`lookup_timeout_ms` to 250–30,000 ms. Direct lookup text is limited to 4,096
characters. `open-ref` accepts at most 64 KiB of UTF-8 query input and
`open-text` at most 8 MiB of UTF-8 body input. The normal configured inline-full
default is 262,144 characters and its safety clamp is 2,097,152 characters so
even four-byte UTF-8 text fits `open-text`; larger bodies use the opaque-ref
path.

## Runtime files

Open-full files, optional retained envelopes, and the optional idle-exit Unix
socket live under `$XDG_RUNTIME_DIR/dank-lookup` (private `/tmp` fallback).
Directories are `0700`, files/socket are `0600`, writes are same-directory
atomic replacements. Files created by `open-text`/`open-ref` are scheduled for
automatic deletion after 300 seconds by default (test/advanced override:
`DANK_LOOKUP_FULL_TTL_SECONDS`). The opener starts the same CLI's hidden
`cleanup-file` helper with only an opaque basename, TTL, and file-identity
metadata. The helper runs in a new session and deletes only the original,
owned, mode-0600 regular file after its age reaches the TTL; a same-name atomic
replacement is not removed. Startup and Open paths retain opportunistic stale
cleanup. Optional retained envelopes have a separate manual/stale cleanup path:

```bash
dank-lookup cleanup --max-age SECONDS
```

There is no unversioned `latest.json` update detector in the standalone path.
