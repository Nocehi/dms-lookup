import QtQuick
import QtQuick.Templates as T

Item {
    id: root

    required property var controller
    required property var theme

    signal closeRequested
    signal copyRequested
    signal openFullRequested

    function alpha(color, amount) {
        return Qt.rgba(color.r, color.g, color.b, amount)
    }

    function hasCjkOrKana(text) {
        for (var i = 0; i < text.length; ++i) {
            var code = text.charCodeAt(i)
            if ((code >= 0x3400 && code <= 0x9fff)
                    || (code >= 0x3040 && code <= 0x30ff))
                return true
        }
        return false
    }

    Rectangle {
        anchors.fill: parent
        color: root.theme.surface_container_high
        radius: 18
        border.color: root.theme.outline
        border.width: 1

        Item {
            id: header
            anchors.top: parent.top
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.margins: 16
            height: 58

            Text {
                id: queryLabel
                anchors.left: parent.left
                anchors.right: closeButton.left
                anchors.rightMargin: 12
                anchors.verticalCenter: parent.verticalCenter
                text: root.controller.queryText !== ""
                      ? root.controller.queryText : "Reading selection…"
                color: root.theme.on_surface
                // Qt's font.family performs platform fallback when the
                // preferred family is absent; `font.families` is not available
                // in the supported Quickshell Qt build.
                font.family: root.hasCjkOrKana(text) ? "Asebi Mincho"
                                                     : "Noto Serif"
                font.pixelSize: root.hasCjkOrKana(text) ? 25 : 23
                maximumLineCount: 2
                elide: Text.ElideRight
                wrapMode: Text.Wrap
            }

            T.Button {
                id: closeButton
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                implicitWidth: 34
                implicitHeight: 34
                hoverEnabled: true

                contentItem: Text {
                    text: "×"
                    color: root.theme.on_surface
                    font.pixelSize: 20
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }

                background: Rectangle {
                    radius: 17
                    color: closeButton.hovered
                           ? root.alpha(root.theme.primary, 0.20)
                           : "transparent"
                }

                onClicked: root.closeRequested()
            }
        }

        Flickable {
            id: modeStrip
            anchors.top: header.bottom
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.leftMargin: 16
            anchors.rightMargin: 16
            height: root.controller.availableModes.length > 0 ? 38 : 0
            visible: height > 0
            clip: true
            contentWidth: modeRow.width
            contentHeight: height
            boundsBehavior: Flickable.StopAtBounds

            Row {
                id: modeRow
                height: parent.height
                spacing: 6

                Repeater {
                    model: root.controller.availableModes

                    T.Button {
                        id: modeButton
                        required property string modelData
                        property var modeResult: root.controller.resultsByMode[modelData]
                                                   || ({})
                        implicitWidth: Math.max(70, modeText.implicitWidth + 24)
                        implicitHeight: 32
                        hoverEnabled: true

                        contentItem: Text {
                            id: modeText
                            text: modeButton.modeResult.label || modeButton.modelData
                            color: root.controller.activeMode === modeButton.modelData
                                   ? root.theme.on_primary : root.theme.on_surface
                            font.pixelSize: 12
                            font.weight: Font.Medium
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }

                        background: Rectangle {
                            radius: 10
                            color: root.controller.activeMode === modeButton.modelData
                                   ? root.theme.primary
                                   : (modeButton.hovered
                                      ? root.alpha(root.theme.primary, 0.16)
                                      : "transparent")
                            border.color: root.controller.activeMode === modeButton.modelData
                                          ? "transparent" : root.theme.outline
                            border.width: root.controller.activeMode === modeButton.modelData
                                          ? 0 : 1
                        }

                        onClicked: root.controller.applyMode(modelData)
                    }
                }
            }
        }

        Flickable {
            id: tokenStrip
            anchors.top: modeStrip.bottom
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.leftMargin: 16
            anchors.rightMargin: 16
            height: root.controller.segmented && root.controller.tokenItems.length > 0
                    ? 36 : 0
            visible: height > 0
            clip: true
            contentWidth: tokenRow.width
            contentHeight: height
            boundsBehavior: Flickable.StopAtBounds

            Row {
                id: tokenRow
                height: parent.height
                spacing: 6

                T.Button {
                    id: wholeButton
                    implicitWidth: wholeText.implicitWidth + 20
                    implicitHeight: 28
                    hoverEnabled: true

                    contentItem: Text {
                        id: wholeText
                        text: "Whole"
                        color: root.theme.on_surface
                        opacity: root.controller.activeToken === "" ? 1.0 : 0.70
                        font.pixelSize: 11
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }

                    background: Rectangle {
                        radius: 9
                        color: root.controller.activeToken === ""
                               ? root.alpha(root.theme.primary, 0.22)
                               : (wholeButton.hovered
                                  ? root.alpha(root.theme.primary, 0.12)
                                  : "transparent")
                    }

                    onClicked: root.controller.selectToken("")
                }

                Repeater {
                    model: root.controller.tokenItems

                    T.Button {
                        id: tokenButton
                        required property var modelData
                        property string tokenTextValue: String(modelData.token || "")
                        property int tokenIndex: Number(modelData.index)
                        property string declaredKey: String(modelData.key || "")
                        property string resultKey: root.controller.tokenItemResultKey(
                                                       declaredKey,
                                                       tokenTextValue,
                                                       tokenIndex)
                        property bool hasResult: resultKey !== ""
                        implicitWidth: Math.max(42, tokenText.implicitWidth + 20)
                        implicitHeight: 28
                        hoverEnabled: hasResult
                        enabled: hasResult

                        contentItem: Text {
                            id: tokenText
                            text: tokenButton.tokenTextValue
                            color: root.theme.on_surface
                            opacity: !tokenButton.hasResult ? 0.38
                                     : (root.controller.activeTokenKey
                                        === tokenButton.resultKey ? 1.0 : 0.72)
                            font.pixelSize: 12
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }

                        background: Rectangle {
                            radius: 9
                            color: root.controller.activeTokenKey === tokenButton.resultKey
                                   ? root.alpha(root.theme.primary, 0.22)
                                   : (tokenButton.hovered
                                      ? root.alpha(root.theme.primary, 0.12)
                                      : "transparent")
                        }

                        onClicked: root.controller.selectTokenKey(resultKey)
                    }
                }
            }
        }

        Item {
            id: metadata
            anchors.top: tokenStrip.bottom
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.leftMargin: 18
            anchors.rightMargin: 18
            height: 34

            Text {
                anchors.left: parent.left
                anchors.right: availabilityLabel.left
                anchors.rightMargin: 8
                anchors.verticalCenter: parent.verticalCenter
                text: {
                    var kind = root.controller.showRelated ? "Related" : "Exact"
                    var mode = root.controller.activeLabel()
                    var source = root.controller.activeSubtitle()
                    var pieces = [kind]
                    if (mode)
                        pieces.push(mode)
                    if (source)
                        pieces.push(source)
                    return pieces.join(" · ")
                }
                color: root.theme.on_surface
                opacity: 0.64
                font.pixelSize: 11
                elide: Text.ElideRight
            }

            Text {
                id: availabilityLabel
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                width: Math.min(190, implicitWidth)
                text: root.controller.activeTruncated()
                      || root.controller.activeFullRef() !== ""
                      ? (root.controller.activeFullChars() > 0
                         ? "Preview · full entry: "
                           + root.controller.activeFullChars() + " chars"
                         : "Preview · full entry available")
                      : (root.controller.incompleteModes.length > 0
                         ? "Timed out: "
                           + root.controller.incompleteModes.join(", ")
                         : (root.controller.unavailableModes.length > 0
                            ? "Unavailable: "
                              + root.controller.unavailableModes.join(", ") : ""))
                color: root.theme.primary
                opacity: 0.82
                font.pixelSize: 10
                elide: Text.ElideRight
                horizontalAlignment: Text.AlignRight
            }
        }

        Rectangle {
            id: divider
            anchors.top: metadata.bottom
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.leftMargin: 16
            anchors.rightMargin: 16
            height: 1
            color: root.alpha(root.theme.outline, 0.45)
        }

        Item {
            id: bodyArea
            anchors.top: divider.bottom
            anchors.bottom: actions.top
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.margins: 16

            Column {
                anchors.centerIn: parent
                width: Math.min(parent.width, 260)
                spacing: 14
                visible: root.controller.phase === "loading"

                Text {
                    width: parent.width
                    text: root.controller.queryText === ""
                          ? "Reading selected text…" : "Looking up locally…"
                    color: root.theme.on_surface
                    opacity: 0.74
                    font.pixelSize: 13
                    horizontalAlignment: Text.AlignHCenter
                }

                Rectangle {
                    width: parent.width
                    height: 3
                    radius: 2
                    color: root.alpha(root.theme.outline, 0.28)

                    Rectangle {
                        id: loadingBar
                        width: parent.width * 0.32
                        height: parent.height
                        radius: 2
                        color: root.theme.primary

                        SequentialAnimation on x {
                            running: root.controller.phase === "loading"
                            loops: Animation.Infinite
                            NumberAnimation {
                                from: 0
                                to: loadingBar.parent.width - loadingBar.width
                                duration: 220
                                easing.type: Easing.InOutCubic
                            }
                            NumberAnimation {
                                from: loadingBar.parent.width - loadingBar.width
                                to: 0
                                duration: 220
                                easing.type: Easing.InOutCubic
                            }
                        }
                    }
                }
            }

            Text {
                anchors.centerIn: parent
                width: Math.min(parent.width, 420)
                text: root.controller.errorText
                visible: root.controller.phase === "error"
                color: root.theme.on_surface
                opacity: 0.85
                font.pixelSize: 13
                wrapMode: Text.WordWrap
                horizontalAlignment: Text.AlignHCenter
            }

            Text {
                anchors.centerIn: parent
                width: Math.min(parent.width, 420)
                text: root.controller.emptyStateMessage()
                visible: root.controller.phase === "ready"
                         && root.controller.activeBody() === ""
                color: root.theme.on_surface
                opacity: 0.55
                font.pixelSize: 13
                horizontalAlignment: Text.AlignHCenter
            }

            Flickable {
                id: definitionScroll
                anchors.fill: parent
                visible: root.controller.phase === "ready"
                         && root.controller.activeBody() !== ""
                clip: true
                contentWidth: width
                contentHeight: definitionText.height
                boundsBehavior: Flickable.StopAtBounds
                flickableDirection: Flickable.VerticalFlick

                Text {
                    id: definitionText
                    width: definitionScroll.width
                    text: root.controller.activeBody()
                    textFormat: Text.PlainText
                    color: root.theme.on_surface
                    font.pixelSize: root.hasCjkOrKana(root.controller.queryText)
                                    ? 15 : 14
                    lineHeightMode: Text.ProportionalHeight
                    lineHeight: root.hasCjkOrKana(root.controller.queryText)
                                ? 1.48 : 1.40
                    wrapMode: Text.Wrap
                }

                T.ScrollBar.vertical: T.ScrollBar {
                    policy: T.ScrollBar.AsNeeded
                }
            }

            Connections {
                target: root.controller

                function onScrollResetRequested() {
                    definitionScroll.contentY = 0
                }
            }
        }

        Item {
            id: actions
            anchors.bottom: parent.bottom
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.leftMargin: 16
            anchors.rightMargin: 16
            height: 50

            Row {
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                spacing: 6
                visible: root.controller.phase === "ready"
                         && root.controller.hasRelated()

                T.Button {
                    id: exactButton
                    implicitWidth: 70
                    implicitHeight: 30
                    hoverEnabled: true

                    contentItem: Text {
                        text: "Exact"
                        color: root.controller.showRelated
                               ? root.theme.on_surface : root.theme.on_primary
                        font.pixelSize: 11
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }

                    background: Rectangle {
                        radius: 9
                        color: !root.controller.showRelated
                               ? root.theme.primary
                               : (exactButton.hovered
                                  ? root.alpha(root.theme.primary, 0.14)
                                  : "transparent")
                    }

                    onClicked: root.controller.setRelated(false)
                }

                T.Button {
                    id: relatedButton
                    implicitWidth: 78
                    implicitHeight: 30
                    hoverEnabled: true

                    contentItem: Text {
                        text: "Related"
                        color: root.controller.showRelated
                               ? root.theme.on_primary : root.theme.on_surface
                        font.pixelSize: 11
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }

                    background: Rectangle {
                        radius: 9
                        color: root.controller.showRelated
                               ? root.theme.primary
                               : (relatedButton.hovered
                                  ? root.alpha(root.theme.primary, 0.14)
                                  : "transparent")
                    }

                    onClicked: root.controller.setRelated(true)
                }
            }

            Row {
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                spacing: 6
                visible: root.controller.phase === "ready"
                         && root.controller.activeBody() !== ""

                T.Button {
                    id: copyButton
                    implicitWidth: 68
                    implicitHeight: 30
                    hoverEnabled: true

                    contentItem: Text {
                        text: "Copy"
                        color: root.theme.on_surface
                        font.pixelSize: 11
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }

                    background: Rectangle {
                        radius: 9
                        color: copyButton.hovered
                               ? root.alpha(root.theme.primary, 0.16)
                               : "transparent"
                        border.color: root.theme.outline
                        border.width: 1
                    }

                    onClicked: root.copyRequested()
                }

                T.Button {
                    id: openButton
                    implicitWidth: 88
                    implicitHeight: 30
                    hoverEnabled: true

                    contentItem: Text {
                        text: "Open full"
                        color: root.theme.on_surface
                        font.pixelSize: 11
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }

                    background: Rectangle {
                        radius: 9
                        color: openButton.hovered
                               ? root.alpha(root.theme.primary, 0.16)
                               : "transparent"
                        border.color: root.theme.outline
                        border.width: 1
                    }

                    onClicked: root.openFullRequested()
                }
            }
        }
    }
}
