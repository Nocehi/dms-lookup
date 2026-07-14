import QtQuick

QtObject {
    id: root

    // The standalone theme contract. Adapters may supply these exact keys.
    property bool dark: true
    property var paletteOverride: ({})

    readonly property color primary: colorOr(paletteOverride["primary"],
                                              dark ? "#c9b8ff" : "#5f43a8")
    readonly property color on_primary: colorOr(paletteOverride["on_primary"],
                                                 dark ? "#2f1767" : "#ffffff")
    readonly property color surface: colorOr(paletteOverride["surface"],
                                              dark ? "#121116" : "#fffbff")
    readonly property color surface_container_high: colorOr(
                                                        paletteOverride["surface_container_high"],
                                                        dark ? "#2b292f" : "#e9e1eb")
    readonly property color on_surface: colorOr(paletteOverride["on_surface"],
                                                 dark ? "#e8e0e9" : "#1d1a20")
    readonly property color outline: colorOr(paletteOverride["outline"],
                                              dark ? "#958e99" : "#78717c")

    function colorOr(value, fallback) {
        var candidate = String(value || "").trim()
        // Matugen's hex output is deliberately the only accepted external form.
        // Rejecting arbitrary strings keeps a partially-written palette from
        // replacing a known-good built-in color.
        if (/^#[0-9a-fA-F]{6}$/.test(candidate)
                || /^#[0-9a-fA-F]{8}$/.test(candidate))
            return candidate
        return fallback
    }

    function applyPalette(candidate) {
        if (!candidate || typeof candidate !== "object")
            return false

        var required = ["primary", "on_primary", "surface",
                        "surface_container_high", "on_surface", "outline"]
        var next = ({})
        for (var i = 0; i < required.length; ++i) {
            var key = required[i]
            var value = String(candidate[key] || "").trim()
            if (!/^#[0-9a-fA-F]{6}$/.test(value)
                    && !/^#[0-9a-fA-F]{8}$/.test(value))
                return false
            next[key] = value
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
