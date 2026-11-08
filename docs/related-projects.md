# Related projects

Dank Lookup is one of several local projects on the same niri/DMS desktop.
These projects live outside this repository and are not installed by it, but
changes here or there can affect each other. This page records what they are
and where they touch Dank Lookup.

## LyX (locally built)

LyX 2.5.3 is built from an AUR PKGBUILD in `~/lyx` and runs as a native
Wayland Qt 6 application (`QT_QPA_PLATFORM=wayland;xcb`).

**Interaction with Dank Lookup: selection capture.** Unpatched LyX does not
make selected text available on the Wayland primary selection. To save work,
`GuiSelection::haveSelection()` (`src/frontends/qt/GuiSelection.cpp`) only
claims PRIMARY with an empty placeholder; the real text is filled in on
`XCB_SELECTION_REQUEST` (`src/frontends/qt/GuiApplication.cpp`), which exists
only under X11. Under Wayland a client reading PRIMARY gets an empty string, so
`dank-lookup-selection --capture` reports `success_empty` and no card opens,
for both the hotkey and a deep press. This was measured on 2026-11-08: drag,
double-click, Shift+arrow, and CJK selections in LyX all gave empty PRIMARY,
while selections in other applications gave `success_nonempty`.

This is a LyX limitation, not a Dank Lookup bug. Dank Lookup does not add an
LyX-specific workaround and keeps its rules: it reads PRIMARY once on demand,
and a deep press never falls back to the clipboard.

Ways to get lookup working in LyX:

- **Local LyX patch (chosen).** A patch carried in the `~/lyx` PKGBUILD's
  `prepare()` offers PRIMARY lazily on Wayland: on selection it registers a
  `QMimeData` subclass whose `retrieveData()` reads the current selection only
  when another client asks. This matches LyX's X11 behaviour and avoids
  recomputing large selections on every cursor move. With no active selection
  it returns an empty string rather than the previous text. The patch lives on a
  local branch and must be reapplied when the AUR package is updated.
- `QT_QPA_PLATFORM=xcb lyx` runs LyX through XWayland (xwayland-satellite).
  Untested whether PRIMARY reaches Wayland clients this way; HiDPI and fcitx5
  input may get worse.
- `"selection": {"clipboard_fallback": true}` plus Ctrl+C before the hotkey.
  The hotkey only; a deep press never reads the clipboard.

## LyX card shell (Scrivener-like card wall)

`~/lyx/cardwall/` holds a card wall for LyX documents. `lyxcards.py` reads
`.lyx` files read-only and turns each heading into a card with a synopsis.
With `--serve` it can jump the open LyX window to a card through the LyXServer
pipe (`~/.lyx/lyxpipe`). `lyxowner.py` reads inotify watch lists from `/proc`
to find which LyX process has a file open.

**Interaction with Dank Lookup.** The card shell itself never reads PRIMARY or
the clipboard. However, a card jump briefly selects the heading paragraph
(`heading_fingerprint()` runs `paragraph-select`, reads statistics, then
`line-begin`). With the lazy LyX patch above, that selection is gone before any
client asks for PRIMARY, so a later lookup sees an empty selection and never
the heading text. A patch that wrote PRIMARY eagerly on every selection change
would instead replace the user's selection with the heading on each jump; do
not change the LyX patch in that direction without checking the card shell.

## When changing things

- Changing how Dank Lookup reads the selection (`bin/dank-lookup-selection`,
  [`privacy.md`](privacy.md)): re-test a lookup from LyX.
- Changing the LyX patch or rebuilding LyX: re-test a lookup from LyX and a card
  jump from the card shell.
