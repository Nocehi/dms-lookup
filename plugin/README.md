# Dank Lookup — optional DMS compatibility wrapper

The product frontend is the standalone Quickshell config. This directory keeps
a thin Dank Material Shell 1.4.6+ wrapper for users who want DMS theme colors or
need to preserve an existing DMS keybind.

The wrapper is a `daemon` plugin, not a DMS launcher provider. It does not
implement launcher `trigger`, `getItems`, or `executeItem` hooks. Its only DMS
dependency is the six-color theme adapter; lookup state, UI, and backend process
handling are shared with the standalone frontend.

## Runtime contract

- IPC target: `dank-lookup-dms`
- Show the loading card: `dms ipc dank-lookup-dms prepare selection`
- Submit the selected text using the returned request id:
  `dms ipc dank-lookup-dms lookupPrepared REQUEST_ID QUERY selection`
- One-call fallback: `dms ipc dank-lookup-dms lookup QUERY`
- Reload after installing repository files: `dms ipc plugins reload dankLookup`

The two-stage form lets the first frame render before the selection command
finishes. Arguments are passed as argv; neither the plugin nor shared QML uses
`sh -c`, file polling, or a shared `latest.json`.

## Processes and privacy

The manifest declares `process` permission because explicit lookup, Copy, and
Open full actions spawn `dank-lookup` or `wl-copy`. The wrapper does not request
network permission, monitor the clipboard, write query history, or emit query
logs. For bounded inline content, `Open full` sends the active full body to
`dank-lookup open-text` over stdin. A larger entry stays out of the lookup JSON:
the wrapper passes its opaque `full_ref` to `dank-lookup open-ref` as argv and
sends the query over bounded UTF-8 stdin for an in-memory re-lookup. The helpers
own the private, request-scoped runtime file and schedule its deletion after 300
seconds by default. A re-lookup can fail if dictionaries or config changed in
the meantime. Normal lookup still passes the query as a safe argv value; only
the oversized Open helper avoids adding it to that helper's argv. QML never
interpolates a path, query, or content into a shell command.

This directory is not a live deployment. Copying or editing it does not reload
an installed DMS instance.
