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
    signal copyTextRequested(string text)
    signal openFullRequested

    function alpha(color, amount) {
        return root.theme.withAlpha(color, amount)
    }

    // Card chrome follows the system locale: Traditional Chinese under zh*,
    // English otherwise. Dictionary text is never translated.
    readonly property bool chineseUi: Qt.locale().name.indexOf("zh") === 0

    function ui(english, chinese) {
        return root.chineseUi ? chinese : english
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
                      ? root.controller.queryText
                      : root.ui("Reading selection…", "讀取選取文字…")
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

                Accessible.name: root.ui("Close", "關閉")

                contentItem: Item {
                    Repeater {
                        model: [45, -45]

                        Rectangle {
                            required property real modelData
                            anchors.centerIn: parent
                            width: root.theme.closeIconSize
                            height: root.theme.iconStroke
                            radius: root.theme.radiusFull
                            rotation: modelData
                            color: root.theme.on_surface
                            antialiasing: true
                        }
                    }
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

        Text {
            id: pronunciationLabel
            anchors.top: header.bottom
            anchors.topMargin: root.theme.spacingXS
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.leftMargin: root.theme.popoutPadding
            anchors.rightMargin: root.theme.popoutPadding
            text: root.controller.phase === "ready"
                  ? root.controller.activeDisplay.pronunciation : ""
            visible: text !== ""
            color: root.theme.on_surface
            opacity: root.theme.textSecondary
            font.family: root.theme.fontUi
            font.pixelSize: root.theme.fontSizeMedium
            elide: Text.ElideRight
        }

        Flickable {
            id: modeStrip
            // Anchor past the pronunciation only while it shows; a height
            // binding on that Text made a binding loop.
            anchors.top: pronunciationLabel.visible ? pronunciationLabel.bottom : header.bottom
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
                            radius: root.theme.radiusSmall
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
                        text: root.ui("Whole", "整句")
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
            anchors.topMargin: visible ? root.theme.spacingS : 0
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.leftMargin: root.theme.popoutPadding
            anchors.rightMargin: root.theme.popoutPadding
            visible: sourceLabel.text !== "" || availabilityLabel.text !== ""
            height: visible ? Math.max(sourceLabel.implicitHeight,
                                       availabilityLabel.implicitHeight) : 0

            Text {
                id: sourceLabel
                anchors.left: parent.left
                anchors.right: availabilityLabel.left
                anchors.rightMargin: root.theme.spacingS
                anchors.verticalCenter: parent.verticalCenter
                // Each section names its own dictionary and the mode chip names
                // the mode, so this line only marks related results and names
                // the source of an unlabeled (flat) body.
                text: {
                    var pieces = []
                    if (root.controller.activeIsDetection())
                        pieces.push(root.ui("Local detection", "本機偵測"))
                    else if (root.controller.showRelated)
                        pieces.push(root.ui("Related results", "相關結果"))
                    var sections = root.controller.activeDisplay.sections
                    var source = root.controller.activeSubtitle()
                    if (source && sections.length === 1 && sections[0].source === "")
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
                         ? root.ui("Preview · full entry: "
                                   + root.controller.activeFullChars() + " chars",
                                   "預覽 · 完整條目 "
                                   + root.controller.activeFullChars() + " 字")
                         : root.ui("Preview · full entry available", "預覽 · 另有完整條目"))
                      : (incomplete !== ""
                         ? incomplete
                         : (root.controller.unavailableModes.length > 0
                            ? root.ui("Unavailable: ", "無法使用：")
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
                          ? root.ui("Reading selected text…", "讀取選取文字…")
                          : root.ui("Looking up locally…", "本機查詢中…")
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

            Rectangle {
                id: detectionSwatch
                anchors.top: parent.top
                anchors.right: parent.right
                width: root.theme.controlHeight
                height: root.theme.controlHeight
                visible: root.controller.phase === "ready"
                         && root.controller.activeSwatch() !== ""
                // Data, not design: the detected colour itself is the content.
                color: visible ? root.controller.activeSwatch() : "transparent"
                radius: root.theme.radiusSmall
                border.color: root.theme.borderColor
                border.width: root.theme.borderWidth
                z: 1
            }

            Flickable {
                id: definitionScroll
                anchors.fill: parent
                visible: root.controller.phase === "ready"
                         && root.controller.activeBody() !== ""
                clip: true
                contentWidth: width
                contentHeight: sectionColumn.height
                boundsBehavior: Flickable.StopAtBounds
                flickableDirection: Flickable.VerticalFlick

                Column {
                    id: sectionColumn
                    width: definitionScroll.width
                    spacing: root.theme.spacingL

                    Repeater {
                        model: root.controller.activeDisplay.sections

                        Column {
                            id: section
                            required property var modelData
                            width: sectionColumn.width
                            spacing: root.theme.spacingS

                            // Dictionary heading with a hairline to the right,
                            // as in a dictionary page: secondary text, no accent.
                            Item {
                                width: parent.width
                                height: sourceHeading.implicitHeight
                                visible: section.modelData.source !== ""

                                Text {
                                    id: sourceHeading
                                    anchors.left: parent.left
                                    anchors.verticalCenter: parent.verticalCenter
                                    width: Math.min(implicitWidth, parent.width)
                                    text: section.modelData.source
                                    color: root.theme.on_surface
                                    opacity: root.theme.textSecondary
                                    font.family: root.theme.fontUi
                                    font.pixelSize: root.theme.fontSizeMedium
                                    font.weight: Font.Medium
                                    elide: Text.ElideRight
                                }

                                Rectangle {
                                    anchors.left: sourceHeading.right
                                    anchors.leftMargin: root.theme.spacingM
                                    anchors.right: parent.right
                                    anchors.verticalCenter: parent.verticalCenter
                                    height: root.theme.borderWidth
                                    color: root.theme.dividerColor
                                }
                            }

                            Text {
                                width: parent.width
                                textFormat: section.modelData.styled ? Text.StyledText
                                                                     : Text.PlainText
                                text: section.modelData.styled
                                      ? root.controller.styledMarkup(section.modelData.lines,
                                                                     root.theme.styledDimColor)
                                      : section.modelData.text
                                color: root.theme.on_surface
                                font.family: root.theme.fontUi
                                font.pixelSize: root.theme.fontSizeMedium
                                lineHeightMode: Text.ProportionalHeight
                                lineHeight: root.theme.bodyLineHeight
                                wrapMode: Text.Wrap
                            }
                        }
                    }
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
                            text: relationButton.modelData ? root.ui("Related", "相關")
                                                           : root.ui("Exact", "精確")
                            color: relationButton.selected ? root.theme.on_primary
                                                           : root.theme.on_surface
                            font.family: root.theme.fontUi
                            font.pixelSize: root.theme.fontSizeSmall
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }

                        background: Rectangle {
                            radius: root.theme.radiusSmall
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

                // Copy is a text button; Open full is the tonal (secondary
                // container) button, as in DMS. Pill shape: DMS buttons up to
                // 40 px use corner "m", which is a full pill at this height.
                Repeater {
                    // A detection offers one Copy per detected value and no
                    // Open full; dictionary results keep Copy + Open full.
                    model: {
                        if (!root.controller.activeIsDetection())
                            return ["copy", "open"]
                        var choices = root.controller.activeCopyChoices()
                        if (choices.length <= 1)
                            return ["copy"]
                        return choices.map(function(choice, i) { return "copy:" + i })
                    }

                    T.Button {
                        id: actionButton
                        required property string modelData
                        property bool tonal: modelData === "open"
                        required property int index
                        implicitWidth: Math.max(root.theme.minControlWidth,
                                                actionText.implicitWidth
                                                + 2 * root.theme.spacingL)
                        implicitHeight: root.theme.controlHeight
                        hoverEnabled: true

                        contentItem: Text {
                            id: actionText
                            text: {
                                if (actionButton.tonal)
                                    return root.ui("Open full", "完整條目")
                                var copy = root.ui("Copy", "複製")
                                if (actionButton.modelData.indexOf("copy:") !== 0)
                                    return copy
                                var choice = root.controller.activeCopyChoices()[actionButton.index]
                                return choice ? copy + " " + choice.label : copy
                            }
                            color: actionButton.tonal ? root.theme.on_secondary_container
                                                      : root.theme.primary
                            font.family: root.theme.fontUi
                            font.pixelSize: root.theme.fontSizeSmall
                            font.weight: Font.Medium
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }

                        background: Rectangle {
                            radius: root.theme.radiusFull
                            color: actionButton.tonal
                                   ? root.theme.secondary_container
                                   : "transparent"

                            Rectangle {
                                anchors.fill: parent
                                radius: parent.radius
                                color: actionButton.tonal ? root.theme.on_secondary_container
                                                          : root.theme.primary
                                opacity: actionButton.down ? root.theme.stateSelected
                                         : (actionButton.hovered ? root.theme.stateHover : 0)
                            }
                        }

                        onClicked: {
                            if (root.controller.activeIsDetection())
                                root.copyTextRequested(
                                    root.controller.activeCopyText(actionButton.index))
                            else if (actionButton.tonal)
                                root.openFullRequested()
                            else
                                root.copyRequested()
                        }
                    }
                }
            }
        }
    }
}
