import QtQuick
import QtQuick.Effects
import QtQuick.Templates as T

// Every colour, radius, spacing step, type size and font family comes from the
// LookupTheme token file (`theme`); tests/test_design_tokens.py enforces it.
Item {
    id: root

    required property var controller
    required property var theme

    signal closeRequested
    signal copyRequested
    signal openFullRequested

    function alpha(color, amount) {
        return root.theme.withAlpha(color, amount)
    }

    // Elevation level 2 (DMS popout baseline). RectangularShadow needs Qt 6.9+.
    RectangularShadow {
        anchors.fill: card
        visible: root.theme.shadowEnabled
        radius: card.radius
        offset: Qt.vector2d(0, root.theme.shadowOffsetY)
        blur: root.theme.shadowBlur
        color: root.theme.shadowColor
    }

    Rectangle {
        id: card
        anchors.fill: parent
        anchors.margins: root.theme.shadowPadding
        color: root.theme.cardColor
        radius: root.theme.windowRadius
        border.color: root.theme.borderColor
        border.width: root.theme.borderWidth

        Item {
            id: header
            anchors.top: parent.top
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.margins: root.theme.popoutPadding
            height: Math.max(root.theme.iconButtonSize, queryLabel.implicitHeight)

            Text {
                id: queryLabel
                anchors.left: parent.left
                anchors.right: closeButton.left
                anchors.rightMargin: root.theme.gap
                anchors.verticalCenter: parent.verticalCenter
                text: root.controller.queryText !== ""
                      ? root.controller.queryText : "Reading selection…"
                color: root.theme.on_surface
                // Jigmo for CJK headwords (DANK_LOOKUP_HEADWORD_FONT overrides),
                // Asebi Mincho for other headings. `font.families` is not
                // available in the supported Quickshell Qt build.
                font.family: root.theme.headingFamily(text, root.controller.headwordFontFamily)
                font.pixelSize: root.theme.hasCjkOrKana(text)
                                ? root.theme.fontSizeXXLarge : root.theme.fontSizeXLarge
                maximumLineCount: root.theme.headingMaxLines
                elide: Text.ElideRight
                wrapMode: Text.Wrap
            }

            T.Button {
                id: closeButton
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                implicitWidth: root.theme.iconButtonSize
                implicitHeight: root.theme.iconButtonSize
                hoverEnabled: true

                contentItem: Text {
                    text: "×"
                    color: root.theme.on_surface
                    font.family: root.theme.fontUi
                    font.pixelSize: root.theme.fontSizeXLarge
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }

                background: Rectangle {
                    radius: root.theme.radiusFull
                    color: closeButton.hovered
                           ? root.alpha(root.theme.primary, root.theme.stateHover)
                           : "transparent"
                }

                onClicked: root.closeRequested()
            }
        }

        Flickable {
            id: modeStrip
            anchors.top: header.bottom
            anchors.topMargin: visible ? root.theme.gap : 0
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.leftMargin: root.theme.popoutPadding
            anchors.rightMargin: root.theme.popoutPadding
            height: root.controller.availableModes.length > 0 ? root.theme.controlHeight : 0
            visible: height > 0
            clip: true
            contentWidth: modeRow.width
            contentHeight: height
            boundsBehavior: Flickable.StopAtBounds

            Row {
                id: modeRow
                height: parent.height
                spacing: root.theme.spacingS

                Repeater {
                    model: root.controller.availableModes

                    T.Button {
                        id: modeButton
                        required property string modelData
                        property var modeResult: root.controller.resultsByMode[modelData]
                                                   || ({})
                        property bool selected: root.controller.activeMode === modelData
                        implicitWidth: Math.max(root.theme.minControlWidth,
                                                modeText.implicitWidth + 2 * root.theme.spacingL)
                        implicitHeight: root.theme.controlHeight
                        hoverEnabled: true

                        contentItem: Text {
                            id: modeText
                            text: modeButton.modeResult.label || modeButton.modelData
                            color: modeButton.selected ? root.theme.on_primary
                                                       : root.theme.on_surface
                            font.family: root.theme.fontUi
                            font.pixelSize: root.theme.fontSizeSmall
                            font.weight: Font.Medium
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }

                        background: Rectangle {
                            radius: root.theme.cornerRadius
                            color: modeButton.selected
                                   ? root.theme.primary
                                   : (modeButton.hovered
                                      ? root.alpha(root.theme.primary, root.theme.stateHover)
                                      : "transparent")
                            border.color: modeButton.selected ? "transparent"
                                                              : root.theme.borderColor
                            border.width: modeButton.selected ? 0 : root.theme.borderWidth
                        }

                        onClicked: root.controller.applyMode(modelData)
                    }
                }
            }
        }

        Flickable {
            id: tokenStrip
            anchors.top: modeStrip.bottom
            anchors.topMargin: visible ? root.theme.spacingS : 0
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.leftMargin: root.theme.popoutPadding
            anchors.rightMargin: root.theme.popoutPadding
            height: root.controller.segmented && root.controller.tokenItems.length > 0
                    ? root.theme.chipHeight : 0
            visible: height > 0
            clip: true
            contentWidth: tokenRow.width
            contentHeight: height
            boundsBehavior: Flickable.StopAtBounds

            Row {
                id: tokenRow
                height: parent.height
                spacing: root.theme.spacingXS

                T.Button {
                    id: wholeButton
                    implicitWidth: wholeText.implicitWidth + 2 * root.theme.spacingM
                    implicitHeight: root.theme.chipHeight
                    hoverEnabled: true

                    contentItem: Text {
                        id: wholeText
                        text: "Whole"
                        color: root.theme.on_surface
                        opacity: root.controller.activeToken === "" ? 1.0
                                                                    : root.theme.textSecondary
                        font.family: root.theme.fontUi
                        font.pixelSize: root.theme.fontSizeSmall
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }

                    background: Rectangle {
                        radius: root.theme.radiusSmall
                        color: root.controller.activeToken === ""
                               ? root.alpha(root.theme.primary, root.theme.stateSelected)
                               : (wholeButton.hovered
                                  ? root.alpha(root.theme.primary, root.theme.stateHover)
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
                        // A token cut short by the lookup budget is marked, not
                        // presented as a plain miss.
                        property bool timedOut: root.controller.incompleteTokens.indexOf(
                                                    declaredKey) >= 0
                        implicitWidth: Math.max(root.theme.minChipWidth,
                                                tokenText.implicitWidth + 2 * root.theme.spacingM)
                        implicitHeight: root.theme.chipHeight
                        hoverEnabled: hasResult
                        enabled: hasResult

                        contentItem: Text {
                            id: tokenText
                            text: tokenButton.tokenTextValue
                            color: tokenButton.timedOut ? root.theme.error : root.theme.on_surface
                            opacity: !tokenButton.hasResult
                                     ? (tokenButton.timedOut ? root.theme.textSecondary
                                                             : root.theme.textDisabled)
                                     : (root.controller.activeTokenKey
                                        === tokenButton.resultKey ? 1.0 : root.theme.textSecondary)
                            font.family: root.theme.fontUi
                            font.pixelSize: root.theme.fontSizeSmall
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }

                        background: Rectangle {
                            radius: root.theme.radiusSmall
                            color: root.controller.activeTokenKey === tokenButton.resultKey
                                   ? root.alpha(root.theme.primary, root.theme.stateSelected)
                                   : (tokenButton.hovered
                                      ? root.alpha(root.theme.primary, root.theme.stateHover)
                                      : "transparent")
                            border.color: tokenButton.timedOut
                                          ? root.alpha(root.theme.error, root.theme.outlineOpacity)
                                          : "transparent"
                            border.width: tokenButton.timedOut ? root.theme.borderWidth : 0
                        }

                        onClicked: root.controller.selectTokenKey(resultKey)
                    }
                }
            }
        }

        Item {
            id: metadata
            anchors.top: tokenStrip.bottom
            anchors.topMargin: root.theme.spacingS
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.leftMargin: root.theme.popoutPadding
            anchors.rightMargin: root.theme.popoutPadding
            height: Math.max(sourceLabel.implicitHeight, availabilityLabel.implicitHeight)

            Text {
                id: sourceLabel
                anchors.left: parent.left
                anchors.right: availabilityLabel.left
                anchors.rightMargin: root.theme.spacingS
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
                opacity: root.theme.textSecondary
                font.family: root.theme.fontUi
                font.pixelSize: root.theme.fontSizeSmall
                elide: Text.ElideRight
            }

            Text {
                id: availabilityLabel
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                width: Math.min(root.theme.statusMaxWidth, implicitWidth)
                property string incomplete: root.controller.incompleteLabel()
                text: root.controller.activeTruncated()
                      || root.controller.activeFullRef() !== ""
                      ? (root.controller.activeFullChars() > 0
                         ? "Preview · full entry: "
                           + root.controller.activeFullChars() + " chars"
                         : "Preview · full entry available")
                      : (incomplete !== ""
                         ? incomplete
                         : (root.controller.unavailableModes.length > 0
                            ? "Unavailable: "
                              + root.controller.unavailableModes.join(", ") : ""))
                color: incomplete !== "" && text === incomplete ? root.theme.error
                                                                : root.theme.primary
                font.family: root.theme.fontUi
                font.pixelSize: root.theme.fontSizeSmall
                elide: Text.ElideRight
                horizontalAlignment: Text.AlignRight
            }
        }

        Rectangle {
            id: divider
            anchors.top: metadata.bottom
            anchors.topMargin: root.theme.spacingS
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.leftMargin: root.theme.popoutPadding
            anchors.rightMargin: root.theme.popoutPadding
            height: root.theme.borderWidth
            color: root.theme.dividerColor
        }

        Item {
            id: bodyArea
            anchors.top: divider.bottom
            anchors.bottom: actions.top
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.topMargin: root.theme.gap
            anchors.bottomMargin: root.theme.gap
            anchors.leftMargin: root.theme.popoutPadding
            anchors.rightMargin: root.theme.popoutPadding

            Column {
                anchors.centerIn: parent
                width: Math.min(parent.width, root.theme.loadingWidth)
                spacing: root.theme.gap
                visible: root.controller.phase === "loading"

                Text {
                    width: parent.width
                    text: root.controller.queryText === ""
                          ? "Reading selected text…" : "Looking up locally…"
                    color: root.theme.on_surface
                    opacity: root.theme.textSecondary
                    font.family: root.theme.fontUi
                    font.pixelSize: root.theme.fontSizeMedium
                    horizontalAlignment: Text.AlignHCenter
                }

                Rectangle {
                    width: parent.width
                    height: root.theme.progressHeight
                    radius: root.theme.radiusFull
                    color: root.alpha(root.theme.outline, root.theme.outlineOpacity)

                    Rectangle {
                        id: loadingBar
                        width: parent.width * 0.32
                        height: parent.height
                        radius: root.theme.radiusFull
                        color: root.theme.primary

                        SequentialAnimation on x {
                            running: root.controller.phase === "loading"
                            loops: Animation.Infinite
                            NumberAnimation {
                                from: 0
                                to: loadingBar.parent.width - loadingBar.width
                                duration: root.theme.loadingSweepDuration
                                easing.type: Easing.InOutCubic
                            }
                            NumberAnimation {
                                from: loadingBar.parent.width - loadingBar.width
                                to: 0
                                duration: root.theme.loadingSweepDuration
                                easing.type: Easing.InOutCubic
                            }
                        }
                    }
                }
            }

            Text {
                anchors.centerIn: parent
                width: Math.min(parent.width, root.theme.messageMaxWidth)
                text: root.controller.errorText
                visible: root.controller.phase === "error"
                color: root.theme.error
                font.family: root.theme.fontUi
                font.pixelSize: root.theme.fontSizeMedium
                wrapMode: Text.WordWrap
                horizontalAlignment: Text.AlignHCenter
            }

            Text {
                anchors.centerIn: parent
                width: Math.min(parent.width, root.theme.messageMaxWidth)
                text: root.controller.emptyStateMessage()
                visible: root.controller.phase === "ready"
                         && root.controller.activeBody() === ""
                color: root.theme.on_surface
                opacity: root.theme.textTertiary
                font.family: root.theme.fontUi
                font.pixelSize: root.theme.fontSizeMedium
                wrapMode: Text.WordWrap
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
                    font.family: root.theme.fontUi
                    font.pixelSize: root.theme.fontSizeMedium
                    lineHeightMode: Text.ProportionalHeight
                    lineHeight: root.theme.bodyLineHeight
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
            anchors.margins: root.theme.popoutPadding
            height: root.theme.controlHeight

            Row {
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                spacing: root.theme.spacingS
                visible: root.controller.phase === "ready"
                         && root.controller.hasRelated()

                Repeater {
                    model: [false, true]

                    T.Button {
                        id: relationButton
                        required property bool modelData
                        property bool selected: root.controller.showRelated === modelData
                        implicitWidth: Math.max(root.theme.minControlWidth,
                                                relationText.implicitWidth
                                                + 2 * root.theme.spacingL)
                        implicitHeight: root.theme.controlHeight
                        hoverEnabled: true

                        contentItem: Text {
                            id: relationText
                            text: relationButton.modelData ? "Related" : "Exact"
                            color: relationButton.selected ? root.theme.on_primary
                                                           : root.theme.on_surface
                            font.family: root.theme.fontUi
                            font.pixelSize: root.theme.fontSizeSmall
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }

                        background: Rectangle {
                            radius: root.theme.cornerRadius
                            color: relationButton.selected
                                   ? root.theme.primary
                                   : (relationButton.hovered
                                      ? root.alpha(root.theme.primary, root.theme.stateHover)
                                      : "transparent")
                        }

                        onClicked: root.controller.setRelated(modelData)
                    }
                }
            }

            Row {
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                spacing: root.theme.spacingS
                visible: root.controller.phase === "ready"
                         && root.controller.activeBody() !== ""

                Repeater {
                    model: ["Copy", "Open full"]

                    T.Button {
                        id: actionButton
                        required property string modelData
                        implicitWidth: Math.max(root.theme.minControlWidth,
                                                actionText.implicitWidth
                                                + 2 * root.theme.spacingL)
                        implicitHeight: root.theme.controlHeight
                        hoverEnabled: true

                        contentItem: Text {
                            id: actionText
                            text: actionButton.modelData
                            color: root.theme.on_surface
                            font.family: root.theme.fontUi
                            font.pixelSize: root.theme.fontSizeSmall
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }

                        background: Rectangle {
                            radius: root.theme.cornerRadius
                            color: actionButton.hovered
                                   ? root.alpha(root.theme.primary, root.theme.stateHover)
                                   : "transparent"
                            border.color: root.theme.borderColor
                            border.width: root.theme.borderWidth
                        }

                        onClicked: actionButton.modelData === "Copy"
                                   ? root.copyRequested() : root.openFullRequested()
                    }
                }
            }
        }
    }
}
