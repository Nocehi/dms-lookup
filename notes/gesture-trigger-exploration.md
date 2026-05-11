---
title: Gesture trigger exploration
status: research-notes
scope: touchpad integration, fusuma evaluation, tap watcher
created: 2026-05-11
---

## Summary

Explored adding touchpad gesture trigger (multi-finger tap) for
dank-lookup-selection, evaluated fusuma vs lighter alternatives. Concluded:
Python script parsing `libinput debug-events` is the best starting point.
A 4-finger tap watcher is deployed to `~/.local/bin/dank-lookup-tap-watcher`
for a 2-week trial before considering repo inclusion.

## Environment snapshot (2026-05-11)

| Item | Value |
|------|-------|
| niri | 26.04 (8ed0da4) |
| libinput (library) | 1.31.1, installed |
| `libinput debug-events` CLI | **not installed** -- needs `libinput-tools` package (~580 KB) |
| gtk4 | 4.22.4, already installed (libinput-tools won't pull new deps) |
| python-evdev | 1.9.3, already installed (831 KB) |
| ruby | **not installed** (~100 MB with gems) |
| `input` group | user **not in group** -- required for /dev/input access |
| Touchpad | BLTP7853:00 347D:7853, /dev/input/event6, perms 660:input |
| AUR packages | ruby-fusuma 3.10.0, ruby-fusuma-plugin-tap 0.4.2 available |
| niri touchpad config | `tap` enabled (tap-to-click), `natural-scroll` |
| Current trigger | `Mod+Alt+D repeat=false` in dms/binds.kdl |

## Fusuma evaluation

### Architecture (worth understanding)

Fusuma's pipeline (`lib/fusuma.rb`):
```
input -> filter -> parse -> buffer -> detect -> merge -> execute
```

Plugin auto-discovery via `Base.inherited` hook + `Manager.add`. External
plugins are Ruby gems matching `fusuma-plugin-*`; they only implement the
pipeline stages they need (e.g. fusuma-plugin-tap adds parser + buffer +
detector but no input or executor).

### Why fusuma was rejected

1. **Ruby runtime cost**: ~100 MB for a single gesture binding. No other
   Ruby use on this system.

2. **fusuma-plugin-tap `hold` detection has a hardcoded 0.7s floor.**

   In `tap_detector.rb:83`:
   ```ruby
   def hold?(buffer, holding_time)
     return false if holding_time < 0.7
   ```

   This means the user-facing `threshold.hold` config value **cannot reduce
   the hold time below 0.7 seconds**. For option C (3-finger hold ~500ms as
   a macOS Force Click analogue), fusuma would silently ignore the 0.5s
   threshold and require 0.7s instead. This is undocumented behavior --
   the config appears to accept any value but the detector overrides it.

   This was a key reason for rejecting fusuma: the tool's config model
   promises tunability it does not deliver, and fixing it requires forking
   the detector gem.

3. **AUR packaging vs gem install**: `ruby-fusuma` AUR package exists, but
   `sudo gem install` (as commonly documented) risks version conflicts with
   AUR-managed gems. Correct approach would be AUR-only, but that ties
   updates to AUR maintainer cadence.

## Gesture options evaluated

### A. 4-finger tap (recommended, no conflicts)

libinput tap-to-click maps: 1-finger=left, 2-finger=right, 3-finger=middle.
4-finger has **no mapping** -- niri receives no click event. Clean.

### B. 3-finger tap (macOS Look Up analogue, conflict)

Produces middle-click via libinput tap-to-click. niri passes middle-click to
focused window (terminal paste, Firefox new-tab). No per-finger-count tap
control in niri -- mitigating requires disabling all tap-to-click or
compositor-level input filtering. Not worth the breakage.

### C. 3-finger hold ~500ms (no conflict, but fusuma floor issue)

libinput's tap state machine times out after ~180ms hold, so no click is
produced. Clean at the niri level. But fusuma-plugin-tap hardcodes 0.7s
minimum (see above), making the 500ms target unreachable without a fork.

A custom Python watcher could implement 500ms hold detection, but adds
complexity vs the simpler 4-finger tap.

## Implementation alternatives

### Python + libinput debug-events (chosen)

~60 lines. Parses tap state machine text from `libinput debug-events
--verbose --enable-tap`. Leverages libinput's mature palm rejection and
tap detection. Requires `libinput-tools` (580 KB, no new transitive deps).

Risk: `debug-events` text format is not a stable API. Format changes in
libinput upgrades would break the regex. Historically rare for this output.

### Python + python-evdev (raw evdev)

~50 lines core, but needs additional logic for palm rejection, motion
threshold, and dynamic device discovery. Zero new package deps. Risk:
reimplementing what libinput already does well.

### libinput record (structured output)

Outputs YAML/JSON but **does not expose the tap state machine**. Would
still need custom tap detection logic. No advantage over raw evdev. Rejected.

### Keep Mod+Alt+D only

Zero deps, zero maintenance. The keyboard shortcut already works. Gesture
is a UX experiment, not a necessity.

## Trial plan

`~/.local/bin/dank-lookup-tap-watcher` deployed for personal use (not in
repo). Observation period: ~2 weeks. Signals to collect before considering
repo inclusion:

- [ ] libinput debug-events format stability across libinput updates
- [ ] Tap state machine edge cases (missed taps, phantom triggers)
- [ ] Suspend/resume: does watcher recover cleanly?
- [ ] False positive rate during normal 4-finger gestures (if any)
- [ ] systemd user service restart behavior

## Related

- [future/per-mode-config-fallback.md](future/per-mode-config-fallback.md) -- fusuma's 3-layer config pattern
- [future/per-app-context.md](future/per-app-context.md) -- fusuma's per-app context mechanism
