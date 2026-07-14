import QtQuick
import Quickshell
import Quickshell.Io
import qs.Common
import qs.Modules.Plugins
import "shared" as Shared

// Compatibility wrapper only. Lookup state, rendering, process execution, and
// IPC ordering live in the same qs-free shared implementation as standalone.
PluginComponent {
    id: root
    pluginId: "dankLookup"

    QtObject {
        id: dmsTheme

        readonly property color primary: Theme.primary
        readonly property color on_primary: Theme.onPrimary
        readonly property color surface: Theme.surface
        readonly property color surface_container_high: Theme.surfaceContainerHigh
        readonly property color on_surface: Theme.surfaceText
        readonly property color outline: Theme.outlineMedium
        readonly property bool dark: !Theme.isLightMode
    }

    Shared.LookupApplication {
        ipcTarget: "dank-lookup-dms"
        themeOverride: dmsTheme
    }
}
