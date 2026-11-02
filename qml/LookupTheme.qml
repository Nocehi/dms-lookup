import QtQuick

// The single design-token file shared by the standalone frontend and the
// optional DMS wrapper. Every colour, radius, spacing step, type size, font
// family, surface/outline opacity, elevation and window metric used by the
// card is defined here; tests/test_design_tokens.py rejects radius numbers,
// hex colours and font-family strings elsewhere in QML.
//
// Baseline: DMS 2.0 popout at radiusStrength 75 and fontScale 1.04
// (Theme.cornerRadiusS/cornerRadius/windowRadius = 12/18/24,
// fontSizeSmall..XXLarge = 12/15/17/21/29, popupTransparency 0.9,
// elevationLevel2 at the default intensity 12 / opacity 30).
//
// `host` may supply any token by name (the DMS wrapper maps DMS Theme values
// onto these names); `paletteOverride` holds a validated matugen palette.
QtObject {
    id: root

    property bool dark: true
    property var paletteOverride: ({})
    property var host: null

    // ---- Palette -----------------------------------------------------------
    // Matugen role names. The first six are required from a matugen file; the
    // rest fall back per key so older generated files stay usable.
    readonly property var requiredPaletteKeys: ["primary", "on_primary", "surface",
                                                "surface_container_high", "on_surface",
                                                "outline"]
    readonly property var optionalPaletteKeys: ["error", "on_error"]

    // Built-in palettes used without matugen/DMS (Material 3 baseline roles).
    readonly property var fallbackPalettes: ({
        "dark": {
            "primary": "#c9b8ff",
            "on_primary": "#2f1767",
            "surface": "#121116",
            "surface_container_high": "#2b292f",
            "on_surface": "#e8e0e9",
            "outline": "#958e99",
            "error": "#ffb4ab",
            "on_error": "#690005"
        },
        "light": {
            "primary": "#5f43a8",
            "on_primary": "#ffffff",
            "surface": "#fffbff",
            "surface_container_high": "#e9e1eb",
            "on_surface": "#1d1a20",
            "outline": "#78717c",
            "error": "#ba1a1a",
            "on_error": "#ffffff"
        }
    })

    readonly property color primary: paletteColor("primary")
    readonly property color on_primary: paletteColor("on_primary")
    readonly property color surface: paletteColor("surface")
    readonly property color surface_container_high: paletteColor("surface_container_high")
    readonly property color on_surface: paletteColor("on_surface")
    readonly property color outline: paletteColor("outline")
    readonly property color error: paletteColor("error")
    readonly property color on_error: paletteColor("on_error")

    // ---- Shape ---------------------------------------------------------------
    readonly property real radiusSmall: hostValue("radiusSmall", 12)
    readonly property real cornerRadius: hostValue("cornerRadius", 18)
    readonly property real windowRadius: hostValue("windowRadius", 24)
    // Rectangle clamps a radius to half its shorter side: a full pill/circle.
    readonly property real radiusFull: 9999

    // ---- Spacing -------------------------------------------------------------
    readonly property real spacingXXS: 2
    readonly property real spacingXS: 4
    readonly property real spacingS: 8
    readonly property real spacingM: 12
    readonly property real spacingL: 16
    readonly property real spacingXL: 24
    readonly property real popoutPadding: spacingL
    readonly property real gap: spacingM

    // ---- Component metrics ---------------------------------------------------
    readonly property real controlHeight: 32
    readonly property real chipHeight: 28
    readonly property real iconButtonSize: 32
    readonly property real progressHeight: 3
    readonly property real minChipWidth: 40
    readonly property real minControlWidth: 72
    readonly property real statusMaxWidth: 200
    readonly property real loadingWidth: 260
    readonly property real messageMaxWidth: 420
    readonly property int headingMaxLines: 2

    // ---- Surface, outline, elevation -----------------------------------------
    readonly property real surfaceOpacity: hostValue("surfaceOpacity", 0.9)
    readonly property real outlineOpacity: 0.35
    readonly property real dividerOpacity: 0.35
    readonly property real borderWidth: 1
    readonly property bool shadowEnabled: hostValue("shadowEnabled", true)
    readonly property real shadowOffsetY: hostValue("shadowOffsetY", 4)
    readonly property real shadowBlur: hostValue("shadowBlur", 8)
    readonly property real shadowAlpha: hostValue("shadowAlpha", 0.125)
    // Window margin that keeps the elevation shadow inside the window.
    readonly property real shadowPadding: shadowEnabled
                                          ? Math.ceil(shadowBlur + Math.abs(shadowOffsetY))
                                          : 0
    readonly property color shadowColor: Qt.rgba(0, 0, 0, shadowAlpha)
    readonly property color cardColor: withAlpha(surface, surfaceOpacity)
    readonly property color borderColor: withAlpha(outline, outlineOpacity)
    readonly property color dividerColor: withAlpha(outline, dividerOpacity)

    // State layers (Material 3 hover/selected opacities) and text emphasis.
    readonly property real stateHover: 0.12
    readonly property real stateSelected: 0.22
    readonly property real textSecondary: 0.70
    readonly property real textTertiary: 0.55
    readonly property real textDisabled: 0.38

    // ---- Type ----------------------------------------------------------------
    readonly property real fontSizeSmall: hostValue("fontSizeSmall", 12)
    readonly property real fontSizeMedium: hostValue("fontSizeMedium", 15)
    readonly property real fontSizeLarge: hostValue("fontSizeLarge", 17)
    readonly property real fontSizeXLarge: hostValue("fontSizeXLarge", 21)
    readonly property real fontSizeXXLarge: hostValue("fontSizeXXLarge", 29)
    readonly property real bodyLineHeight: 1.45

    // Headings use Asebi Mincho; CJK headwords use Jigmo, because Asebi maps
    // ~3,000 URO code points (騞, 謋 …) to empty glyphs and Qt never falls back
    // from a glyph the primary font claims (offscreen render, 2026-11-02).
    // Jigmo2/Jigmo3 reach planes 2-3 through fontconfig. UI text uses MiSans TC;
    // monospace uses Sarasa Mono HC for Hong Kong glyph forms.
    readonly property string fontHeading: "Asebi Mincho"
    readonly property string fontHeadingCjk: "Jigmo"
    readonly property string fontUi: "MiSans TC"
    readonly property string fontMono: "Sarasa Mono HC"

    // ---- Window and motion ---------------------------------------------------
    readonly property real windowWidth: 620
    readonly property real windowHeight: 460
    readonly property real windowMinWidth: 440
    readonly property real windowMinHeight: 280
    readonly property real windowMaxWidth: 760
    readonly property real windowMaxHeight: 640
    readonly property int appearDuration: 170
    readonly property int loadingSweepDuration: 220

    // ---- Functions -----------------------------------------------------------
    function hostValue(name, fallback) {
        if (host && host[name] !== undefined && host[name] !== null)
            return host[name]
        return fallback
    }

    function withAlpha(color, amount) {
        return Qt.rgba(color.r, color.g, color.b, amount)
    }

    function isHexColor(value) {
        var candidate = String(value || "").trim()
        return /^#[0-9a-fA-F]{6}$/.test(candidate) || /^#[0-9a-fA-F]{8}$/.test(candidate)
    }

    function paletteColor(key) {
        if (host && host[key] !== undefined && host[key] !== null)
            return host[key]
        var candidate = paletteOverride[key]
        // Matugen's hex output is deliberately the only accepted external form.
        // Rejecting arbitrary strings keeps a partially-written palette from
        // replacing a known-good built-in color.
        if (isHexColor(candidate))
            return String(candidate).trim()
        return fallbackPalettes[dark ? "dark" : "light"][key]
    }

    function headingFamily(text, overrideFamily) {
        if (!hasCjkOrKana(text))
            return fontHeading
        if ((overrideFamily || "") !== "")
            return overrideFamily
        return Qt.fontFamilies().indexOf(fontHeadingCjk) >= 0 ? fontHeadingCjk : fontHeading
    }

    // URO + Ext A, kana, and surrogate pairs of planes 2-3 (Ext B and later).
    function hasCjkOrKana(text) {
        var value = String(text || "")
        for (var i = 0; i < value.length; ++i) {
            var code = value.charCodeAt(i)
            if ((code >= 0x3400 && code <= 0x9fff)
                    || (code >= 0x3040 && code <= 0x30ff)
                    || (code >= 0xd840 && code <= 0xd87f))
                return true
        }
        return false
    }

    function applyPalette(candidate) {
        if (!candidate || typeof candidate !== "object")
            return false

        var next = ({})
        for (var i = 0; i < requiredPaletteKeys.length; ++i) {
            var key = requiredPaletteKeys[i]
            if (!isHexColor(candidate[key]))
                return false
            next[key] = String(candidate[key]).trim()
        }
        for (var j = 0; j < optionalPaletteKeys.length; ++j) {
            var optional = optionalPaletteKeys[j]
            if (isHexColor(candidate[optional]))
                next[optional] = String(candidate[optional]).trim()
        }

        paletteOverride = next
        if (typeof candidate.dark === "boolean")
            dark = candidate.dark
        return true
    }

    function clearPalette() {
        paletteOverride = ({})
    }
}
