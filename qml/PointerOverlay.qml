import QtQuick
import Quickshell
import Quickshell.Wayland

// Pointer placement overlay (DANK_LOOKUP_PLACEMENT=pointer). It lives in its
// own file and LookupApplication loads it by source only in pointer mode:
// a PanelWindow written inside LookupApplication.qml fails the whole config
// at load time on a platform without layer shell (offscreen tests, other
// compositors), even inside an inactive LazyLoader.
//
// Wayland gives a client no global pointer position and no protocol reports
// where the selected text is, so the overlay learns the pointer position from
// the enter event niri sends when the overlay maps under it (niri re-checks
// what is under the pointer every loop). With the pointer hidden (e.g. while
// typing) no enter arrives and the card falls back to the centre after
// pointerWaitMs.
PanelWindow {
    id: pointerOverlay

    // Set by LookupApplication right after the loader creates this window.
    property var app: null
    property var theme: null
    property Component cardComponent: null
    readonly property bool ready: app !== null && theme !== null

    visible: ready && app.overlayShown
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
    readonly property real cardWidth: ready ? theme.windowWidth + 2 * theme.shadowPadding : 0
    readonly property real cardHeight: ready ? theme.windowHeight + 2 * theme.shadowPadding : 0
    readonly property var cardItem: pointerCardLoader.item

    function beginPlacement() {
        placed = false
        pointerWait.restart()
    }

    function placeAt(px, py) {
        if (placed || !ready)
            return
        var margin = theme.screenMargin
        var gapBelow = theme.pointerGap
        var w = cardWidth, h = cardHeight
        var x = px - w / 2
        // Below the pointer when it fits, otherwise above it, like the macOS
        // popover flipping at the screen edge.
        var y = py + gapBelow - theme.shadowPadding
        if (y + h > height - margin)
            y = py - gapBelow - h + theme.shadowPadding
        cardHolder.x = Math.max(margin, Math.min(width - w - margin, x))
        cardHolder.y = Math.max(margin, Math.min(height - h - margin, y))
        placed = true
        pointerWait.stop()
        app.startAppearAnimation()
    }

    function placeCentred() {
        if (!ready)
            return
        placeAt(width / 2, (height - cardHeight) / 2 - theme.pointerGap
                           + theme.shadowPadding)
    }

    Timer {
        id: pointerWait
        interval: pointerOverlay.ready ? pointerOverlay.theme.pointerWaitMs : 0
        onTriggered: pointerOverlay.placeCentred()
    }

    // Blur behind the card only.
    BackgroundEffect.blurRegion: ready && theme.backgroundBlur && pointerOverlay.visible
                                 && pointerOverlay.placed ? overlayBlurRegion : null

    Region {
        id: overlayBlurRegion
        x: cardHolder.x + (pointerOverlay.ready ? pointerOverlay.theme.shadowPadding : 0)
        y: cardHolder.y + (pointerOverlay.ready ? pointerOverlay.theme.shadowPadding : 0)
        width: Math.max(0, cardHolder.width
                           - 2 * (pointerOverlay.ready ? pointerOverlay.theme.shadowPadding : 0))
        height: Math.max(0, cardHolder.height
                            - 2 * (pointerOverlay.ready ? pointerOverlay.theme.shadowPadding : 0))
        radius: pointerOverlay.ready ? pointerOverlay.theme.windowRadius : 0
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
            else if (pointerOverlay.ready)
                pointerOverlay.app.setShown(false)
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
            active: pointerOverlay.cardComponent !== null
            sourceComponent: pointerOverlay.cardComponent
            focus: pointerOverlay.visible
        }
    }
}
