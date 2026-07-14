# Dictionary sources and admission policy

Dank Lookup supports the StarDict ecosystem through local `sdcv`. This
repository ships routing configuration, source-name detection, a status tool,
and conversion helpers only. It ships no `.ifo`, `.idx`, `.syn`, `.dict`,
`.dict.dz`, source CSV, or converted dictionary payload.

The machine-readable policy is
[`language-packs/manifest.json`](../language-packs/manifest.json). Run:

```bash
dank-lookup-language-packs status
dank-lookup-language-packs status --json
```

to compare its exact StarDict names with `sdcv --list-dicts`. A configured mode
is shown as unavailable when none of its dictionaries is installed; users do
not need to author a routing schema to reach that state.

## Admission states

| State | Meaning |
|---|---|
| `unverified` | The repository has not verified the exact payload, conversion chain, current terms, and required notices. Do not bundle or automate its download. |
| `do_not_redistribute` | Known restriction: configuration/detection for an existing local copy only. Never bundle source or converted data. |
| `redistributable` | Reserved for an artifact whose exact license and attribution chain have been admitted. No current manifest entry uses this state. |

A familiar dictionary name, an old note, or an upstream project's general
license is not enough to admit a particular converted StarDict artifact.

## Configured names

| Mode | Exact local StarDict names | Manifest state |
|---|---|---|
| `han` | `萌典重編國語辭典`, `CC-CEDICT 漢英辭典` | unverified |
| `han` | `XDICT漢英辭典` | unknown license; unverified |
| `en` | `wikt-en-en-2025-10-05`, `dictd_www.dict.org_gcide`, `WordNet`, `English-中文 (Zhōngwén) FreeDict+WikDict dictionary` | unverified by this repository |
| `en` | `XDICT英漢辭典` | unknown license; unverified |
| `ja` | `org.edrdg.jmdict-jpn-eng` | unverified exact artifact/attribution chain |
| `ja` | `reader.dict JA` | unknown license; unverified |
| `yue` | `words.hk 粵語詞典` | **do not redistribute** |

The table describes compatibility, not permission. In particular, never
publish words.hk source/converted data, XDICT, or reader.dict through this
project. The prior documentation's confident redistribution claim for 萌典 is
withdrawn pending verification of the exact artifact and terms.

## Local installation

Install only dictionaries whose terms you have checked for your use. StarDict
normally searches these directories:

```text
$HOME/.local/share/stardict/dic
/usr/share/stardict/dic
```

After installing through your trusted package source or a verified manual
process, confirm the exact display name:

```bash
sdcv --list-dicts
dank-lookup-language-packs status
```

If the display name differs, copy the installed default config to your user
config and adjust the `name` field; do not rename dictionary data blindly.

## Definition format

Definition cleanup follows metadata, never an angle-bracket content guess. A
source may explicitly set `"definition_format": "html"` or
`"definition_format": "plain"`. Without an override, Dank Lookup searches the
configured StarDict data directories (and their direct `dic` child), matches
the source `name` exactly to `.ifo` `bookname`, and reads
`sametypesequence`. A single consistent `h` classification selects HTML
cleanup; `m` selects plain cleanup.

Conflicting matches, mixed or unknown sequences, a missing `.ifo`, or a missing
`sametypesequence` all use conservative plain text. That preserves prose such
as `vector<T>`. If `sdcv` has already flattened per-entry field markers and the
`.ifo` omits `sametypesequence`, the original field format cannot be recovered;
the application deliberately does not guess HTML from the definition body.

## Conversion and attribution

`tests/convert_wordshk_csv.py` remains a local conversion helper. Its presence
does not grant permission to obtain, convert, or redistribute source data.

For any newly admitted source, record all of the following before adding an
installer or redistribution claim:

1. exact source and release identifier;
2. exact payload hashes;
3. license text covering that payload and conversion;
4. required attribution/share-alike/notices;
5. converter version and reproducible command;
6. whether modified/converted redistribution is allowed;
7. a negative check proving no dictionary payload entered this repository.
