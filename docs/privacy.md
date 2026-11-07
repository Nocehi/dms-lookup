# Privacy and threat boundary

Dank Lookup's default lookup path is local and offline. It starts no network,
translation, OCR, encyclopedia, LLM, telemetry, or query-history feature.

## What reads selected text

The user-invoked `dank-lookup-selection` launcher sends only source and request
metadata to Quickshell. Quickshell then explicitly starts its bounded capture
mode, which reads the Wayland primary selection once. It reads the regular
clipboard once only when primary is not `success_nonempty` and the user has set
`"selection": {"clipboard_fallback": true}` in `config.json`; the default is
off, and a `force-press` source never falls back, so an accidental deep press
cannot put a copied password or token on screen. Nothing watches or monitors
either selection.

The captured query returns to QML through the capture process pipe. QML writes
a bounded JSON envelope to `dank-lookup backend-request` over stdin; that client
sends one length-prefixed frame to the fixed private user socket
`$XDG_RUNTIME_DIR/dank-lookup/backend.sock`. The socket directory is mode
`0700`, the socket is mode `0600`, and the receiver checks the peer UID. The
query is then written to the tokenizer and `sdcv 0.5.5` over their bounded stdin
protocols. Query-bearing positional CLI arguments and query-bearing QML IPC
methods are rejected or absent; there is no argv compatibility fallback.

The query is never interpolated into `sh -c`, written to a debug log, or
included in benchmark output. For an oversized **Open full** re-fetch, QML
sends the query to `open-ref` over bounded UTF-8 stdin. Copy sends the currently
displayed body to `wl-copy` over stdin.

Selection capture requests the Wayland `text` MIME type and streams at most
16 KiB from each producer. It reports `success_nonempty`, `success_empty`,
`success_whitespace`, `too_large`, `unavailable`, `timeout`, or `command_error`
separately for primary and clipboard (`not_attempted` when the clipboard was not
read). Opt-in clipboard fallback happens only when primary is not
`success_nonempty`; an all-empty or
failed capture becomes a metadata-only UI state and never sends an empty query.
`DANK_LOOKUP_PRIMARY_TIMEOUT_MS` and `DANK_LOOKUP_CLIPBOARD_TIMEOUT_MS` accept
integers from 1 to 5000. Their production defaults remain 250 ms until the
required multi-owner raw dataset, trade-off report, and owner approval exist.

## Local data detectors

`lib/dank_lookup_detectors.py` inspects the query already in the backend
process and returns preview lines. It imports no network, process or file API
(`tests/test_detectors.py` enforces this by AST), fetches no page, title,
favicon, parcel status or exchange rate, and keeps no state. URL previews only
parse the string; the card offers Copy but no Open action. Local time uses the
system time zone through the standard library.

## Runtime data

Runtime helpers use:

```text
$XDG_RUNTIME_DIR/dank-lookup
```

The directory is owned by the current user and mode `0700`; created files are
mode `0600`; the process uses `umask 077` and same-directory atomic replacement.
If `XDG_RUNTIME_DIR` is absent or unsafe, the fallback is the verified private
directory `/tmp/dank-lookup-$UID`, never the shared `/tmp/dank-lookup` path.

Normal private-transport lookups do not retain a query or result file. Choosing **Open full**
either writes the active inline complete body or re-runs a local oversized
lookup in memory, verifies its opaque content hash, and writes only the chosen
complete body. The query is not written. These private viewer files are
scheduled for automatic deletion after 300 seconds by default, including in
the `/tmp` fallback. Cleanup uses the same CLI in a new session with only an
opaque basename, TTL, and original file-identity metadata; it checks the private
root, owner, mode, age, and identity before unlinking, so a same-name replacement
is retained. A re-fetch can fail if the config or dictionaries changed since
the result was rendered.

Request-envelope retention exists only as an explicit compatibility/test
option; it is not the standalone default and uses the separate `dank-lookup
cleanup` stale-file path. Session runtime storage normally disappears with the
user session.

Persistent data is limited to user-installed code, routing configuration, theme
configuration, and dictionaries. Dank Lookup does not install dictionary data.

## Resident process

The optional user service keeps only the Quickshell window/controller resident.
It does not monitor the clipboard, pre-read text, contact the network, or retain
queries. An idle-cleaned fixed-socket backend is cold-started only by an explicit
request. Requests are correlated with a UUID and sequence; cancellation contains
only that metadata. A cancellation received before the server's atomic response
commit wins; one received after commit is acknowledged as unmatched. The
frontend also prevents stale completions from replacing the active result.

Opt-in benchmark mode retains numeric monotonic durations in memory and exposes
them over IPC. It omits query text and result content and writes no telemetry
file.

## Threat boundary

The protections above prevent accidental group/world disclosure and common
shared-`/tmp` races. They do not defend against:

- `root` or a process already running with equivalent tracing/debug privileges;
- a malicious process under the same account that can inspect process state;
- a compromised Wayland compositor or clipboard provider;
- a compromised dictionary, `sdcv`, Quickshell, `wl-copy`, or external viewer;
- content copied elsewhere after the user chooses Copy or Open full.

The DMS wrapper's `process` permission is a manifest disclosure. Current DMS
does not enforce it as an operating-system sandbox, so it is not presented as a
security boundary.
