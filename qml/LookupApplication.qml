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
    readonly property var lookupController: controller
    // "center" (default): a floating window that niri places, centred.
    // "pointer": a transparent overlay on the focused output; the card opens
    // just below the pointer, like the macOS Look Up popover opens under the
    // selected word, and a click outside it closes it.
    readonly property string placement: (Quickshell.env("DANK_LOOKUP_PLACEMENT") || "center")
                                        === "pointer" ? "pointer" : "center"
    // The centre window keeps its own visible flag (niri may close it); the
    // overlay is shown through overlayShown. setShown() drives both.
    property bool overlayShown: false
    readonly property bool shown: placement === "pointer" ? overlayShown : popup.visible
    readonly property var card: placement === "pointer" ? pointerCardLoader.item
                                                        : centerCardLoader.item

    width: 0
    height: 0
    visible: false

    LookupTheme {
        id: lookupTheme
        host: root.themeHost
        requestedWidth: Number(Quickshell.env("DANK_LOOKUP_CARD_WIDTH") || 0)
        requestedHeight: Number(Quickshell.env("DANK_LOOKUP_CARD_HEIGHT") || 0)
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
        windowVisible: root.shown
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
        onCloseRequested: root.setShown(false)
        onTestCopyRequested: root.copyActiveBody()
        onTestOpenRequested: root.openActiveFullBody()
    }

    function showPopup() {
        if (!root.shown) {
            if (root.card) {
                root.card.opacity = 0.35
                root.card.scale = 0.985
            }
            if (root.placement === "pointer")
                pointerOverlay.beginPlacement()
            root.setShown(true)
            if (root.placement !== "pointer")
                appearAnimation.restart()
        } else if (root.card) {
            // An already-open popup receives the new loading/result state in
            // place. No debounce and no animation gate delay the update.
            root.card.opacity = 1
            root.card.scale = 1
        }
    }

    function setShown(value) {
        if (root.placement === "pointer")
            root.overlayShown = value
        else
            popup.visible = value
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

    // The card with its shadow margin and Esc handling; each window loads one.
    Component {
        id: cardComponent

        FocusScope {
            id: cardScope
            property alias lookupCard: lookupCard
            focus: true

            Keys.onPressed: function(event) {
                if (event.key === Qt.Key_Escape) {
                    root.setShown(false)
                    event.accepted = true
                } else if (event.matches(StandardKey.Copy)
                           && root.lookupController.phase === "ready"
                           && root.lookupController.activeBody() !== "") {
                    root.copyActiveBody()
                    event.accepted = true
                }
            }

            LookupCard {
                id: lookupCard
                anchors.fill: parent
                controller: root.lookupController
                theme: root.activeTheme

                onCloseRequested: root.setShown(false)
                onCopyRequested: root.copyActiveBody()
                onCopyTextRequested: function(text) { root.copyText(text) }
                onOpenFullRequested: root.openActiveFullBody()
            }
        }
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

        Loader {
            id: centerCardLoader
            anchors.fill: parent
            active: root.placement === "center"
            sourceComponent: cardComponent
            focus: popup.visible
        }
    }

    // Pointer placement. Wayland gives a client no global pointer position and
    // no protocol reports where the selected text is, so the overlay learns the
    // pointer position from the enter event niri sends when the overlay maps
    // under it (niri re-checks what is under the pointer every loop). With the
    // pointer hidden (e.g. while typing) no enter arrives and the card falls
    // back to the centre after pointerWaitMs.
    PanelWindow {
        id: pointerOverlay
        visible: root.overlayShown
        color: "transparent"
        anchors.top: true
        anchors.bottom: true
        anchors.left: true
        anchors.right: true
        WlrLayershell.layer: WlrLayer.Overlay
        WlrLayershell.namespace: "dank-lookup"
        WlrLayershell.exclusionMode: ExclusionMode.Ignore
        WlrLayershell.keyboardFocus: visible ? WlrKeyboardFocus.Exclusive
                                             : WlrKeyboardFocus.None

        property bool placed: false
        readonly property real cardWidth: lookupTheme.windowWidth + 2 * lookupTheme.shadowPadding
        readonly property real cardHeight: lookupTheme.windowHeight + 2 * lookupTheme.shadowPadding

        function beginPlacement() {
            placed = false
            pointerWait.restart()
        }

        function placeAt(px, py) {
            if (placed)
                return
            var margin = lookupTheme.screenMargin
            var gapBelow = lookupTheme.pointerGap
            var w = cardWidth, h = cardHeight
            var x = px - w / 2
            // Below the pointer when it fits, otherwise above it, like the
            // macOS popover flipping at the screen edge.
            var y = py + gapBelow - lookupTheme.shadowPadding
            if (y + h > height - margin)
                y = py - gapBelow - h + lookupTheme.shadowPadding
            cardHolder.x = Math.max(margin, Math.min(width - w - margin, x))
            cardHolder.y = Math.max(margin, Math.min(height - h - margin, y))
            placed = true
            pointerWait.stop()
            appearAnimation.restart()
        }

        function placeCentred() {
            placeAt(width / 2, (height - cardHeight) / 2 - lookupTheme.pointerGap
                               + lookupTheme.shadowPadding)
        }

        Timer {
            id: pointerWait
            interval: lookupTheme.pointerWaitMs
            onTriggered: pointerOverlay.placeCentred()
        }

        // Blur behind the card only.
        BackgroundEffect.blurRegion: lookupTheme.backgroundBlur && pointerOverlay.visible
                                     && pointerOverlay.placed ? overlayBlurRegion : null

        Region {
            id: overlayBlurRegion
            x: cardHolder.x + lookupTheme.shadowPadding
            y: cardHolder.y + lookupTheme.shadowPadding
            width: Math.max(0, cardHolder.width - 2 * lookupTheme.shadowPadding)
            height: Math.max(0, cardHolder.height - 2 * lookupTheme.shadowPadding)
            radius: lookupTheme.windowRadius
        }

        // Covers the output: the first pointer report places the card, and a
        // press outside the card closes it.
        MouseArea {
            anchors.fill: parent
            hoverEnabled: true
            acceptedButtons: Qt.AllButtons
            onPositionChanged: function(mouse) { pointerOverlay.placeAt(mouse.x, mouse.y) }
            onEntered: if (containsMouse) pointerOverlay.placeAt(mouseX, mouseY)
            onPressed: function(mouse) {
                if (!pointerOverlay.placed)
                    pointerOverlay.placeAt(mouse.x, mouse.y)
                else
                    root.setShown(false)
            }
        }

        Item {
            id: cardHolder
            width: pointerOverlay.cardWidth
            height: pointerOverlay.cardHeight
            visible: pointerOverlay.placed

            // Swallow presses on the card's own margin and body.
            MouseArea {
                anchors.fill: parent
                acceptedButtons: Qt.AllButtons
            }

            Loader {
                id: pointerCardLoader
                anchors.fill: parent
                active: root.placement === "pointer"
                sourceComponent: cardComponent
                focus: pointerOverlay.visible
            }
        }
    }

    // FloatingWindow in noctalia-qs 0.0.12 does not expose QQuickWindow's
    // polished/frameSwapped signal. FrameAnimation is driven by the rendered
    // frame loop and gives a QML frame-tick proxy for loading/result state;
    // it is not compositor presentation. ElapsedTimer is the monotonic clock.
    FrameAnimation {
        running: root.shown && controller.benchmarkEnabled
                 && (controller.pendingFirstQmlFrameTick
                     || controller.pendingResultQmlFrameTick)
        onTriggered: controller.noteFrame()
    }

    ParallelAnimation {
        id: appearAnimation

        NumberAnimation {
            target: root.card
            property: "opacity"
            from: 0.35
            to: 1
            duration: lookupTheme.appearDuration
            easing.type: Easing.OutCubic
        }

        NumberAnimation {
            target: root.card
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
