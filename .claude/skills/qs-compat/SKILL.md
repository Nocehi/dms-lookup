---
name: qs-compat
description: >-
  Use when writing or modifying QML code for DMS plugins. Contains
  known Quickshell compatibility traps and workarounds.
---
## Known traps

| Trap | Workaround |
|------|-----------|
| `import "."` local file import | NOT supported. Inline all QML |
| `T.Button.cursorShape` | Transparent MouseArea shim, acceptedButtons: Qt.NoButton |
| `Qt.application.clipboard` | Process { command: ["wl-copy"]; stdinEnabled: true } |
| Process stdin lifecycle | p.write(s) then Qt.callLater(p.stdinEnabled = false) |
| PluginComponent template | Use pluginId: "dankLookup" |
| FloatingWindow popup | niri floating rule required |
| console.log | Filtered by DMS. Use console.warn |
| FileView reuse | Stale content. Create fresh instance via Qt.createQmlObject per read |
| IpcHandler | Unavailable (missing shared lib) |
| Qt.application.clipboard | Does not exist in DMS runtime |
| Element ID vs property name | id: foo + property string foo → not an lvalue |

## Reload
- Standard: dms ipc call plugins reload dankLookup
- Full rescan: kill $(pgrep -x qs)
