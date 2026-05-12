import QtQuick
import QtQuick.Templates as T
import Quickshell
import Quickshell.Io
import qs.Common
import qs.Modules.Plugins

PluginComponent {
    id: root
    pluginId: "dankLookup"

    property string lastTimestamp: ""
    property string queryText: ""
    property string backendText: ""
    property string resultSubtitle: ""
    property string resultBody: ""
    property string relatedSubtitle: ""
    property string relatedBody: ""
    property bool showRelated: false
    property string parseError: ""
    property bool dataLoaded: false
    property double lastShowTime: 0

    property string activeMode: "han-en"
    property var availableModes: ["han-en"]
    property var resultsByMode: ({})
    property var relatedByMode: ({})

    function hasCjkOrKana(text) {
        for (var i = 0; i < text.length; i++) {
            var c = text.charCodeAt(i)
            if ((c >= 0x3400 && c <= 0x9fff) || (c >= 0x3040 && c <= 0x30ff))
                return true
        }
        return false
    }

    function titleFontFamily() {
        return hasCjkOrKana(queryText) ? "Asebi Mincho" : "Noto Serif"
    }

    Component.onCompleted: {
        console.warn("[dankLookup] component loaded")
        recordInitialTimestamp()
    }

    Component.onDestruction: {
        console.warn("[dankLookup] component destroyed")
    }

    function readFile() {
        var fv = Qt.createQmlObject(
            'import QtQuick; import Quickshell.Io; FileView { path: "/tmp/dank-lookup/latest.json"; blockLoading: true; blockWrites: true; atomicWrites: true }',
            root
        )
        var raw = fv.text()
        fv.destroy()
        if (raw && raw.trim())
            return JSON.parse(raw)
        return null
    }

    function recordInitialTimestamp() {
        try {
            var data = readFile()
            if (data && data.timestamp) {
                lastTimestamp = data.timestamp
                console.warn("[dankLookup] initial timestamp:", lastTimestamp)
            }
        } catch (e) {
            console.warn("[dankLookup] recordInitialTimestamp error:", e)
        }
    }

    function resetScrollPosition() {
        if (flick)
            flick.contentY = 0
    }

    function applyMode(mode) {
        var r = resultsByMode && resultsByMode[mode]
        if (!r)
            return
        var rel = relatedByMode && relatedByMode[mode]
        activeMode = mode
        resultSubtitle = r.subtitle || ""
        resultBody = r.body || ""
        relatedSubtitle = rel ? (rel.subtitle || "") : ""
        relatedBody = rel ? (rel.body || "") : ""
        showRelated = false
        Qt.callLater(resetScrollPosition)
    }

    function displayBody(text) {
        return (text || "")
            .replace(/\n{3,}/g, "\n\n")
            .trim()
    }

    function activeBody() {
        return showRelated && relatedBody ? relatedBody : resultBody
    }

    function activeSourceLabel() {
        if (showRelated && relatedBody)
            return relatedSubtitle ? "Related: " + relatedSubtitle : "Related"
        return resultSubtitle
    }

    function copyToClipboard() {
        if (!resultBody)
            return
        var p = Qt.createQmlObject(
            'import Quickshell.Io; Process { command: ["wl-copy"]; running: true; stdinEnabled: true }',
            root
        )
        if (p)
            Qt.callLater(function() {
                p.write(resultBody)
                Qt.callLater(function() { p.stdinEnabled = false })
            })
    }

    function openFullSource() {
        if (!resultBody)
            return
        var p = Qt.createQmlObject(
            'import Quickshell.Io; Process { command: ["sh", "-c", "cat > /tmp/dank-lookup/full.txt && xdg-open /tmp/dank-lookup/full.txt"]; running: true; stdinEnabled: true }',
            root
        )
        if (p)
            Qt.callLater(function() {
                p.write(resultBody)
                Qt.callLater(function() { p.stdinEnabled = false })
            })
    }

    function checkForUpdate() {
        try {
            var data = readFile()
            if (!data) return

            var ts = data.timestamp || ""
            if (ts === "" || ts === lastTimestamp)
                return
            if (!data.query || data.query.trim() === "")
                return

            queryText = data.query || ""
            backendText = data.backend || ""
            resultSubtitle = ""
            resultBody = ""
            relatedSubtitle = ""
            relatedBody = ""
            showRelated = false
            parseError = ""
            dataLoaded = true
            resultsByMode = ({})
            relatedByMode = ({})
            availableModes = []
            activeMode = "han-en"

            if (data.results_by_mode) {
                resultsByMode = data.results_by_mode
                relatedByMode = data.related_by_mode || ({})
                availableModes = data.available_modes || Object.keys(data.results_by_mode)
                var dm = data.default_mode || (availableModes.length > 0 ? availableModes[0] : "han-en")
                applyMode(dm)
            } else if (data.results && data.results.length > 0) {
                resultSubtitle = data.results[0].subtitle || ""
                resultBody = data.results[0].body || ""
                Qt.callLater(resetScrollPosition)
            }

            lastTimestamp = ts
            console.warn("[dankLookup] showing popup ts=" + ts + " query=" + queryText)
            var now = Date.now()
            if (now - lastShowTime < 2000) {
                console.warn("[dankLookup] debounced, diff=" + (now - lastShowTime))
                return
            }
            lastShowTime = now
            popup.visible = true
        } catch (e) {
            parseError = "Failed to parse lookup data"
            dataLoaded = true
            console.warn("[dankLookup] checkForUpdate error:", e)
            var n = Date.now()
            if (n - lastShowTime < 2000) {
                console.warn("[dankLookup] debounced (error), diff=" + (n - lastShowTime))
                return
            }
            lastShowTime = n
            popup.visible = true
        }
    }

    FloatingWindow {
        id: popup
        title: "Dank Lookup"
        visible: false
        minimumSize: Qt.size(380, 200)
        implicitWidth: 580
        implicitHeight: 380
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

            Rectangle {
                id: card
                anchors.fill: parent
                color: Theme.surfaceContainerHigh
                radius: Theme.cornerRadius
                border.color: Theme.outlineMedium || "#444444"
                border.width: typeof Theme.outlineMedium !== "undefined" ? 1 : 0

                Column {
                    id: popupContent
                    anchors.fill: parent
                    anchors.margins: 14
                    spacing: 10

                    Item {
                        width: parent.width
                        height: Math.max(queryLabel.implicitHeight, closeBtn.height) + 8

                        Text {
                            id: queryLabel
                            x: 8
                            y: 4
                            width: parent.width - 28 - 24
                            text: root.queryText
                            font.family: root.titleFontFamily()
                            font.pixelSize: root.hasCjkOrKana(root.queryText) ? 26 : 24
                            font.weight: Font.Normal
                            lineHeight: 1.12
                            color: Theme.surfaceText
                            elide: Text.ElideRight
                            maximumLineCount: 2
                            wrapMode: Text.WordWrap
                        }

                        T.Button {
                            id: closeBtn
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.topMargin: 4
                            implicitWidth: 28
                            implicitHeight: 28
                            hoverEnabled: true

                            contentItem: Text {
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                                text: "\u2715"
                                font.pixelSize: 14
                                color: Theme.surfaceText
                            }

                            background: Rectangle {
                                radius: 14
                                color: closeBtn.hovered
                                    ? Theme.withAlpha(Theme.primary, 0.30)
                                    : "transparent"
                            }

                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                acceptedButtons: Qt.NoButton
                                hoverEnabled: true
                            }

                            onClicked: popup.visible = false
                        }
                    }

                    Row {
                        id: modePillRow
                        width: parent.width
                        spacing: 6
                        visible: root.dataLoaded && root.parseError === "" && root.availableModes.length === 1

                        Rectangle {
                            radius: 8
                            height: 26
                            width: pillLabel.implicitWidth + 16
                            color: Theme.withAlpha(Theme.primary, 0.12)

                            Text {
                                id: pillLabel
                                anchors.centerIn: parent
                                text: root.activeSourceLabel()
                                font.pixelSize: 11
                                color: Theme.primary
                            }
                        }

                        T.Button {
                            implicitWidth: 26
                            implicitHeight: 26
                            hoverEnabled: true

                            contentItem: Text {
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                                text: "C"
                                font.pixelSize: 11
                                color: Theme.surfaceText
                            }

                            background: Rectangle {
                                radius: 8
                                color: parent.hovered
                                    ? Theme.withAlpha(Theme.primary, 0.15)
                                    : "transparent"
                            }

                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                acceptedButtons: Qt.NoButton
                                hoverEnabled: true
                            }

                            onClicked: root.copyToClipboard()
                        }

                        T.Button {
                            implicitWidth: 26
                            implicitHeight: 26
                            hoverEnabled: true

                            contentItem: Text {
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                                text: "F"
                                font.pixelSize: 11
                                color: Theme.surfaceText
                            }

                            background: Rectangle {
                                radius: 8
                                color: parent.hovered
                                    ? Theme.withAlpha(Theme.primary, 0.15)
                                    : "transparent"
                            }

                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                acceptedButtons: Qt.NoButton
                                hoverEnabled: true
                            }

                            onClicked: root.openFullSource()
                        }
                    }

                    Row {
                        id: segmentedRow
                        width: parent.width
                        spacing: 4
                        visible: root.dataLoaded && root.parseError === "" && root.availableModes.length > 1

                        Repeater {
                            model: root.availableModes

                            Rectangle {
                                width: (segmentedRow.width - (root.availableModes.length - 1) * 4) / Math.max(root.availableModes.length, 1)
                                height: 34
                                radius: 9
                                color: root.activeMode === modelData
                                    ? Theme.primary
                                    : "transparent"

                                Text {
                                    anchors.centerIn: parent
                                    text: (root.resultsByMode[modelData] && root.resultsByMode[modelData].label) || modelData
                                    font.pixelSize: 12
                                    font.weight: Font.Medium
                                    color: root.activeMode === modelData
                                        ? Theme.onPrimary
                                        : Theme.surfaceText
                                }

                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: root.applyMode(modelData)
                                }
                            }
                        }
                    }

                    Text {
                        id: sourceLabel
                        width: parent.width
                        text: root.activeSourceLabel() ? root.activeSourceLabel() : ""
                        font.pixelSize: 11
                        color: Theme.surfaceText
                        opacity: 0.55
                        elide: Text.ElideRight
                        visible: text !== "" && root.dataLoaded
                    }

                    Rectangle {
                        width: parent.width
                        height: 1
                        color: Theme.surfaceText
                        opacity: 0.10
                        visible: root.activeBody() !== "" || root.parseError !== ""
                    }

                    Text {
                        width: parent.width
                        text: root.parseError
                        color: "#ff5252"
                        font.pixelSize: 13
                        visible: root.parseError !== ""
                        wrapMode: Text.WordWrap
                    }

                    Flickable {
                        id: flick
                        width: parent.width
                        height: root.relatedBody !== "" ? 170 : 205
                        clip: true
                        contentHeight: bodyText.height
                        boundsBehavior: Flickable.StopAtBounds
                        visible: root.activeBody() !== ""

                        Text {
                            id: bodyText
                            width: parent.width
                            text: root.displayBody(root.activeBody())
                            font.pixelSize: root.hasCjkOrKana(root.queryText) ? 15 : 14
                            lineHeightMode: Text.ProportionalHeight
                            lineHeight: root.hasCjkOrKana(root.queryText) ? 1.50 : 1.42
                            color: Theme.surfaceText
                            wrapMode: Text.WordWrap
                            textFormat: Text.PlainText
                        }
                    }

                    Rectangle {
                        width: parent.width
                        height: 24
                        radius: 7
                        color: relatedMouse.containsMouse
                            ? Qt.rgba(Theme.primary.r, Theme.primary.g, Theme.primary.b, 0.18)
                            : "transparent"
                        visible: root.relatedBody !== ""

                        Text {
                            anchors.centerIn: parent
                            text: root.showRelated ? "Definition" : "Related"
                            font.pixelSize: 11
                            font.weight: Font.Medium
                            color: Theme.surfaceText
                            opacity: 0.72
                        }

                        MouseArea {
                            id: relatedMouse
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: {
                                root.showRelated = !root.showRelated
                                Qt.callLater(root.resetScrollPosition)
                            }
                        }
                    }

                    Text {
                        width: parent.width
                        horizontalAlignment: Text.AlignRight
                        text: "\u2193"
                        visible: flick.visible && flick.contentHeight > flick.height
                        color: Theme.surfaceText
                        opacity: 0.30
                        font.pixelSize: 11
                    }

                    Text {
                        width: parent.width
                        text: root.dataLoaded && root.queryText
                            ? "No definition found"
                            : "選取文字後按 Mod+Alt+D"
                        color: Theme.surfaceText
                        opacity: 0.5
                        font.pixelSize: 13
                        visible: !root.dataLoaded || (root.activeBody() === "" && root.relatedBody === "" && root.parseError === "")
                        wrapMode: Text.WordWrap
                    }
                }
            }
        }
    }

    Timer {
        id: fileCheckTimer
        interval: 300
        running: !popup.visible
        repeat: true
        onTriggered: checkForUpdate()
    }
}
