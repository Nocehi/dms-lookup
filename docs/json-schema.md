# JSON Contract

dank-lookup communicates between its backend and frontend via two JSON files
in `/tmp/dank-lookup/`. Both are written atomically (`.tmp` then `mv`).

## backend_result.json

Written by `lib/backends/sdcv.sh`. Read by `bin/dank-lookup-selection`.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `backend` | `string` | yes | `"sdcv"` or `"mock"` |
| `title` | `string` | yes | The original query text |
| `default_mode` | `string` | yes | Mode key selected by language classification (e.g. `"han"`, `"en-en"`) |
| `available_modes` | `string[]` | yes | Mode keys that produced results, in config order |
| `subtitle` | `string` | yes | Comma-separated dictionary names for the default mode |
| `body` | `string` | yes | Formatted definition text for the default mode |
| `results_by_mode` | `object` | yes | Per-mode results (see [Mode Result](#mode-result)) |
| `related_by_mode` | `object` | yes | Per-mode fuzzy/related results (same shape as `results_by_mode`). Empty `{}` if none |
| `mode_preferences` | `object` | yes | Per-mode UI hints (see [Mode Preferences](#mode-preferences)) |
| `config` | `object` | yes | Config loading status (see [Config Status](#config-status)) |
| `segmented` | `boolean` | when segmented | `true` if phrase segmentation produced the result |
| `tokens` | `string[]` | when segmented | Token list from the tokenizer |
| `results_by_token` | `object` | when segmented | Per-token lookup results (see [Token Result](#token-result)) |

### Mode Result

Each key in `results_by_mode` and `related_by_mode` is a mode key (e.g. `"han"`, `"en-en"`).

| Field | Type | Description |
|-------|------|-------------|
| `label` | `string` | Display label for the mode (e.g. `"漢語"`, `"英英"`) |
| `subtitle` | `string` | Comma-separated dictionary names that contributed |
| `body` | `string` | Formatted definitions, separated by `\n\n`, each prefixed with `[DictName]\n` |

Body is truncated: max 4000 chars per entry, max 12000 chars total.

### Mode Preferences

Each key matches a mode key in `results_by_mode`.

| Field | Type | Description |
|-------|------|-------------|
| `show_related` | `boolean` | Whether the frontend should display related results by default |

### Config Status

| Field | Type | Description |
|-------|------|-------------|
| `path` | `string \| null` | Config file path attempted, or `null` if none |
| `loaded` | `boolean` | Whether the config file was successfully loaded |
| `warning` | `string \| null` | Parse warning, or `null` |

### Token Result

Each key in `results_by_token` is a token string.

| Field | Type | Description |
|-------|------|-------------|
| `default_mode` | `string \| null` | Best mode for this token's language class |
| `available_modes` | `string[]` | Modes that had results for this token |
| `results_by_mode` | `object` | Same shape as top-level `results_by_mode` |
| `related_by_mode` | `object` | Same shape as top-level `related_by_mode` |

## latest.json

Written by `bin/dank-lookup-selection`. Read by `plugin/DankLookup.qml` (polled via FileView every 300ms).

This wraps `backend_result.json` with selection metadata.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | `string` | yes | The query text (trimmed, max 500 chars) |
| `source` | `string` | yes | `"primary"` (Wayland primary selection) or `"clipboard"` (fallback) |
| `timestamp` | `string` | yes | ISO 8601 timestamp of the lookup |
| `truncated` | `boolean` | yes | `true` if query was truncated to 500 chars |
| `backend` | `string` | yes | Forwarded from backend result |
| `default_mode` | `string \| null` | yes | Forwarded from backend result |
| `available_modes` | `string[] \| null` | yes | Forwarded from backend result |
| `results_by_mode` | `object \| null` | yes | Forwarded from backend result |
| `related_by_mode` | `object \| null` | yes | Forwarded from backend result |
| `mode_preferences` | `object \| null` | yes | Forwarded from backend result |
| `config` | `object \| null` | yes | Forwarded from backend result |
| `segmented` | `boolean \| null` | yes | Forwarded from backend result |
| `tokens` | `string[] \| null` | yes | Forwarded from backend result |
| `results_by_token` | `object \| null` | yes | Forwarded from backend result |
| `results` | `object[]` | yes | Legacy single-result array (see below) |
| `error` | `string \| null` | yes | Error message, or `null` on success |

### Legacy `results` array

Kept for backward compatibility. Contains a single element derived from the default mode.

| Field | Type | Description |
|-------|------|-------------|
| `title` | `string` | Query text |
| `subtitle` | `string` | Dictionary names |
| `body` | `string` | Definition text |

## Detecting updates

The QML frontend detects new lookups by comparing `timestamp` with the
previously seen value. A changed `timestamp` with a non-empty `query` triggers
the popup.
