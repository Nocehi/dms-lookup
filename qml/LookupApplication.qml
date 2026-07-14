import QtQuick
import Quickshell
import Quickshell.Io

Item {
    id: root

    property string ipcTarget: "dank-lookup"
    property string backendCommand: Quickshell.env("DANK_LOOKUP_COMMAND") || "dank-lookup"
    property var themeOverride: null
    readonly property var activeTheme: themeOverride || standaloneTheme

    width: 0
    height: 0
    visible: false

    LookupTheme {
        id: standaloneTheme
        dark: (Quickshell.env("DANK_LOOKUP_THEME_VARIANT") || "dark") !== "light"
    }

    MatugenThemeAdapter {
        theme: standaloneTheme
        path: root.themeOverride ? "" : (Quickshell.env("DANK_LOOKUP_THEME_FILE") || "")
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

    function copyActiveBody() {
        startStdinProcess(stdinProcess, controller.activeBody(),
                          [Quickshell.env("DANK_LOOKUP_WL_COPY") || "wl-copy",
                           "--type", "text/plain;charset=utf-8"])
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
        implicitWidth: 620
        implicitHeight: 460
        minimumSize: Qt.size(440, 280)
        maximumSize: Qt.size(760, 640)
        color: "transparent"

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
            duration: 170
            easing.type: Easing.OutCubic
        }

        NumberAnimation {
            target: card
            property: "scale"
            from: 0.985
            to: 1
            duration: 170
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
