import QtQuick
import Quickshell
import Quickshell.Io
import qs.Common
import qs.Modules.Plugins
import "shared" as Shared

// Compatibility wrapper only. Lookup state, rendering, process execution, and
// IPC ordering live in the same qs-free shared implementation as standalone.
// This object only maps DMS Theme values onto LookupTheme token names; every
// token default, and every token DMS does not supply, stays in LookupTheme.
// Theme property names were verified against the DMS 2.0.0 Theme.qml.
PluginComponent {
    id: root
    pluginId: "dankLookup"

    QtObject {
        id: dmsTheme

        readonly property color primary: Theme.primary
        readonly property color on_primary: Theme.onPrimary
        // DMS popouts draw hostSurface at popupTransparency.
        readonly property color surface: Theme.hostSurface
        readonly property color surface_container_high: Theme.surfaceContainerHigh
        readonly property color on_surface: Theme.surfaceText
        // The raw role. DMS's outlineMedium is scaled by blurLayerOutlineOpacity,
        // which defaults to 0 and made the card border invisible.
        readonly property color outline: Theme.outline
        readonly property color error: Theme.error
        readonly property color on_error: Theme.onError
        readonly property color secondary_container: Theme.secondaryContainer
        readonly property color on_secondary_container: Theme.onSecondaryContainer
        readonly property bool dark: !Theme.isLightMode

        readonly property real radiusSmall: Theme.cornerRadiusS
        readonly property real cornerRadius: Theme.cornerRadius
        readonly property real windowRadius: Theme.windowRadius
        readonly property real surfaceOpacity: Theme.popupTransparency
        readonly property real fontSizeSmall: Theme.fontSizeSmall
        readonly property real fontSizeMedium: Theme.fontSizeMedium
        readonly property real fontSizeLarge: Theme.fontSizeLarge
        readonly property real fontSizeXLarge: Theme.fontSizeXLarge
        readonly property real fontSizeXXLarge: Theme.fontSizeXXLarge
        readonly property bool shadowEnabled: Theme.elevationEnabled
        readonly property real shadowOffsetY: Theme.elevationLevel2.offsetY
        readonly property real shadowBlur: Theme.elevationLevel2.blurPx
        readonly property real shadowAlpha: Theme.elevationLevel2.alpha
    }

    Shared.LookupApplication {
        ipcTarget: "dank-lookup-dms"
        themeHost: dmsTheme
    }
}
