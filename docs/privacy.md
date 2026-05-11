# Privacy

## Default behavior

All processing is local and offline:

- **Selection**: read via `wl-paste` from the Wayland primary selection or clipboard fallback on explicit keypress.
- **Dictionary**: local `sdcv` process. No network request.
- **No daemon**: the plugin does not run background processes.
- **No clipboard monitoring**: clipboard is read only when the user triggers `Mod+Alt+D`.
- **No telemetry**: no analytics, crash reporting, or usage statistics.

## Opt-in features

Network dictionary, translation, OCR, or LLM backends must be opt-in and disabled by default.

### Data exposure

- The primary selection or clipboard may contain sensitive text because they reflect whatever the user selected or copied.
- Dank Lookup processes this text locally only. No text is sent over the network in the default configuration.
- Runtime data is written to `/tmp/dank-lookup/` (tmpfs) and cleared on reboot.
