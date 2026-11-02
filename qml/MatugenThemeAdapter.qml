import QtQuick
import Quickshell.Io

Item {
    id: root

    property var theme: null
    property string path: ""
    property bool active: path !== "" && theme !== null
    readonly property bool loaded: paletteFile.loaded && lastError === ""
    property string lastError: ""

    visible: false
    width: 0
    height: 0

    function paletteObject(document) {
        if (!document || typeof document !== "object")
            return null

        // Accept the small standalone contract directly. A nested `colors`
        // object is also accepted so generic matugen configurations need no
        // Dank Material Shell schema.
        var colors = document.colors && typeof document.colors === "object"
                ? document.colors : document
        return {
            primary: colors.primary,
            on_primary: colors.on_primary || colors.onPrimary,
            surface: colors.surface,
            surface_container_high: colors.surface_container_high
                                    || colors.surfaceContainerHigh,
            on_surface: colors.on_surface || colors.onSurface,
            outline: colors.outline,
            error: colors.error,
            on_error: colors.on_error || colors.onError,
            // matugen's {{mode}} ("dark"/"light") follows the generated scheme;
            // the boolean is the fallback for files without it.
            dark: document.mode === "dark" ? true
                  : document.mode === "light" ? false
                  : typeof document.dark === "boolean" ? document.dark
                  : colors.dark
        }
    }

    function reloadSafely() {
        if (!active)
            return
        try {
            var candidate = paletteObject(JSON.parse(paletteFile.text()))
            if (!candidate || !theme.applyPalette(candidate)) {
                lastError = "palette-contract"
                return
            }
            lastError = ""
        } catch (error) {
            // Keep the last known-good palette on a partial/invalid rewrite.
            lastError = "palette-parse"
        }
    }

    FileView {
        id: paletteFile
        path: root.active ? root.path : ""
        blockLoading: false
        blockWrites: true
        watchChanges: root.active
        printErrors: false

        onLoaded: root.reloadSafely()
        onFileChanged: reload()
        onLoadFailed: function(error) {
            root.lastError = "palette-unavailable"
        }
    }
}
