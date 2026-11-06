import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland

Item {
    id: root

    property string ipcTarget: "dank-lookup"
    property string backendCommand: Quickshell.env("DANK_LOOKUP_COMMAND") || "dank-lookup"
    // Optional token host (the DMS wrapper): supplies LookupTheme token values
    // by name. The tokens themselves always come from the one LookupTheme.
    property var themeHost: null
    readonly property var activeTheme: lookupTheme

    width: 0
    height: 0
    visible: false

    LookupTheme {
        id: lookupTheme
        host: root.themeHost
        dark: root.themeHost && root.themeHost.dark !== undefined
              ? root.themeHost.dark
              : (Quickshell.env("DANK_LOOKUP_THEME_VARIANT") || "dark") !== "light"
    }

    MatugenThemeAdapter {
        theme: lookupTheme
        path: root.themeHost ? "" : (Quickshell.env("DANK_LOOKUP_THEME_FILE") || "")
    }

    LookupController {
        id: controller
        ipcTarget: root.ipcTarget
        backendCommand: root.backendCommand
        windowVisible: popup.visible
        testThemeSnapshot: ({
            primary: String(root.activeTheme.primary),
            on_primary: String(root.activeTheme.on_primary),
            surface: String(root.activeTheme.surface),
            surface_container_high: String(root.activeTheme.surface_container_high),
            on_surface: String(root.activeTheme.on_surface),
            outline: String(root.activeTheme.outline),
            error: String(root.activeTheme.error),
            on_error: String(root.activeTheme.on_error),
            dark: root.activeTheme.dark
        })

        onShowRequested: root.showPopup()
        onCloseRequested: popup.visible = false
        onTestCopyRequested: root.copyActiveBody()
        onTestOpenRequested: root.openActiveFullBody()
    }

    function showPopup() {
        if (!popup.visible) {
            card.opacity = 0.35
            card.scale = 0.985
            popup.visible = true
            appearAnimation.restart()
        } else {
            // An already-open popup receives the new loading/result state in
            // place. No debounce and no animation gate delay the update.
            card.opacity = 1
            card.scale = 1
        }
    }

    function startStdinProcess(component, payload, command) {
        if (payload === "")
            return
        var process = component.createObject(root, {
            payload: payload,
            command: command
        })
        if (process)
            process.running = true
    }

    function copyText(text) {
        startStdinProcess(stdinProcess, String(text || ""),
                          [Quickshell.env("DANK_LOOKUP_WL_COPY") || "wl-copy",
                           "--type", "text/plain;charset=utf-8"])
    }

    // For a local detection this is the detected value (e.g. the ISO date).
    function copyActiveBody() {
        copyText(controller.activeCopyText(0))
    }

    function openActiveFullBody() {
        var spec = controller.activeOpenSpec()
        if (spec.kind === "ref") {
            startStdinProcess(stdinProcess, spec.payload, spec.argv)
            return
        }
        if (spec.kind === "inline")
            startStdinProcess(stdinProcess, spec.payload, spec.argv)
    }

    FloatingWindow {
        id: popup
        title: "Dank Lookup"
        visible: false
        // The window keeps a shadow margin around the card on every side.
        implicitWidth: lookupTheme.windowWidth + 2 * lookupTheme.shadowPadding
        implicitHeight: lookupTheme.windowHeight + 2 * lookupTheme.shadowPadding
        minimumSize: Qt.size(lookupTheme.windowMinWidth + 2 * lookupTheme.shadowPadding,
                             lookupTheme.windowMinHeight + 2 * lookupTheme.shadowPadding)
        maximumSize: Qt.size(lookupTheme.windowMaxWidth + 2 * lookupTheme.shadowPadding,
                             lookupTheme.windowMaxHeight + 2 * lookupTheme.shadowPadding)
        color: "transparent"

        // Blur what is behind the card, like DMS popouts (ext-background-effect;
        // a compositor without it ignores the request). Re-set on every show,
        // because the surface is unmapped while hidden.
        BackgroundEffect.blurRegion: lookupTheme.backgroundBlur && popup.visible
                                     ? cardBlurRegion : null

        Region {
            id: cardBlurRegion
            x: lookupTheme.shadowPadding
            y: lookupTheme.shadowPadding
            width: Math.max(0, popup.width - 2 * lookupTheme.shadowPadding)
            height: Math.max(0, popup.height - 2 * lookupTheme.shadowPadding)
            radius: lookupTheme.windowRadius
        }

        FocusScope {
            anchors.fill: parent
            focus: popup.visible

            Keys.onPressed: function(event) {
                if (event.key === Qt.Key_Escape) {
                    popup.visible = false
                    event.accepted = true
                }
            }

            LookupCard {
                id: card
                anchors.fill: parent
                controller: controller
                theme: root.activeTheme

                onCloseRequested: popup.visible = false
                onCopyRequested: root.copyActiveBody()
                onCopyTextRequested: function(text) { root.copyText(text) }
                onOpenFullRequested: root.openActiveFullBody()
            }
        }
    }

    // FloatingWindow in noctalia-qs 0.0.12 does not expose QQuickWindow's
    // polished/frameSwapped signal. FrameAnimation is driven by the rendered
    // frame loop and gives a QML frame-tick proxy for loading/result state;
    // it is not compositor presentation. ElapsedTimer is the monotonic clock.
    FrameAnimation {
        running: popup.visible && controller.benchmarkEnabled
                 && (controller.pendingFirstQmlFrameTick
                     || controller.pendingResultQmlFrameTick)
        onTriggered: controller.noteFrame()
    }

    ParallelAnimation {
        id: appearAnimation

        NumberAnimation {
            target: card
            property: "opacity"
            from: 0.35
            to: 1
            duration: lookupTheme.appearDuration
            easing.type: Easing.OutCubic
        }

        NumberAnimation {
            target: card
            property: "scale"
            from: 0.985
            to: 1
            duration: lookupTheme.appearDuration
            easing.type: Easing.OutCubic
        }
    }

    Component {
        id: stdinProcess

        Process {
            property string payload: ""
            stdinEnabled: true

            onStarted: {
                write(payload)
                stdinEnabled = false
            }

            onExited: function(exitCode, exitStatus) {
                destroy()
            }
        }
    }

}
