# Theming and design tokens

All design values live in one file, [`qml/LookupTheme.qml`](../qml/LookupTheme.qml),
shared by the standalone frontend and the optional DMS wrapper.
`tests/test_design_tokens.py` fails on a hard-coded radius, a hex/named colour,
an inline `Qt.rgba(...)` or a font-family string anywhere else in QML, and on a
matugen template that misses a palette token.

## Baseline

The defaults reproduce a DMS 2.0 popout at `radiusStrength` 75 and `fontScale`
1.04 (values read from DMS 2.0.0 `Common/Theme.qml` and `DCommon/Common/Shape.js`):

| Group | Tokens | Values |
|---|---|---|
| Radius | `radiusSmall`, `cornerRadius`, `windowRadius`, `radiusFull` | 12, 18, 24, pill |
| Spacing | `spacingXXS` … `spacingXL`; `popoutPadding`, `gap` | 2, 4, 8, 12, 16, 24; 16, 12 |
| Surface | `surfaceOpacity`, `outlineOpacity`, `borderWidth`, `backgroundBlur` | 0.9 of `surface`, 0.35 of `outline`, 1 px, on |
| Elevation | `shadowOffsetY`, `shadowBlur`, `shadowAlpha` | level 2: 0 4 px 8 px, alpha 0.125 |
| Type | `fontSizeSmall` … `fontSizeXXLarge` | 12, 15, 17, 21, 29 |
| Fonts | `fontHeading`, `fontHeadingCjk`, `fontUi`, `fontMono` | Asebi Mincho, Jigmo, MiSans TC, Sarasa Mono HC |

CJK headwords use Jigmo: Asebi Mincho maps about 3,000 URO code points (騞, 謋 …)
to empty glyphs and Qt does not fall back from a glyph the primary font
claims, even when fontconfig removes those code points from Asebi's charset.
`DANK_LOOKUP_HEADWORD_FONT` overrides the CJK headword family. Other heading
text uses Asebi Mincho. Sarasa Mono HC and MiSans TC give Hong Kong/Traditional
glyph forms. The elevation shadow uses `QtQuick.Effects.RectangularShadow`
(Qt 6.9 or newer).

The surface is only 0.9 opaque, so the card asks the compositor to blur what is
behind it (`BackgroundEffect.blurRegion`, ext-background-effect-v1), as DMS
popouts do. A compositor without that protocol ignores the request and the
window behind shows through faintly. The 0.35 outline border matches the
border DMS draws on blurred surfaces (`blurBorderEnabled`, default opacity 0.35).

## Palette

Colours are matugen role names: `primary`, `on_primary`, `surface`,
`surface_container_high`, `on_surface`, `outline` (required in a generated
file) and `error`, `on_error`, `secondary_container`, `on_secondary_container`
(optional; each falls back separately). Built-in
light and dark fallbacks are the `fallbackPalettes` table in the token file.

Sources, in order of precedence:

1. DMS wrapper: `plugin/DankLookup.qml` maps DMS Theme values onto token names
   (`Theme.primary`, `onPrimary`, `hostSurface`, `surfaceContainerHigh`,
   `surfaceText`, `outline`, `error`, `onError`, `isLightMode`,
   `cornerRadiusS`, `cornerRadius`, `windowRadius`, `popupTransparency`,
   `fontSizeSmall` … `fontSizeXXLarge`, `elevationEnabled`,
   `elevationLevel2`). It uses raw `Theme.outline`: `Theme.outlineMedium` is
   scaled by `blurLayerOutlineOpacity`, which defaults to 0.
2. Standalone with matugen: `DANK_LOOKUP_THEME_FILE` names a generated JSON
   file. It is reloaded on change; an invalid or partial rewrite keeps the
   last good palette.
3. Built-in fallback: `DANK_LOOKUP_THEME_VARIANT=light` or `dark` (default).

## Matugen (standalone)

The installer copies the templates to
`~/.local/share/dank-lookup/matugen/{dark,light}-template.json`. Both emit every
palette token plus `"mode": "{{mode}}"`, which follows the scheme matugen
actually generated; the `dark` boolean is only a fallback for files without
`mode`. One template is enough. Add this section to
`~/.config/matugen/config.toml` (the installer never edits it):

```toml
[templates.dank_lookup]
input_path = '~/.local/share/dank-lookup/matugen/dark-template.json'
output_path = '~/.cache/dank-lookup/theme.json'
```

DMS 2.0 runs these user templates whenever it regenerates its theme while
"Run user templates" (`runUserMatugenTemplates`, default on) is enabled. Then
point the resident frontend at the output, for example with
`systemctl --user edit dank-lookup.service`:

```ini
[Service]
Environment=DANK_LOOKUP_THEME_FILE=%h/.cache/dank-lookup/theme.json
```

Verified with matugen 4.2.0: `~` is expanded in both paths, and
`matugen color hex '#6750a4' --mode light` writes `"mode": "light"` from this
template.

## Screenshots

`tests/render_card_screenshots.py` renders the real card offscreen from saved
lookup JSON, for dark and light, optionally through a generated matugen file
(`--theme-file`).
