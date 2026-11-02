# dankLookup Material Design 3 manual

用途：保留 Dank Lookup 詞典卡片的 Material Design 3 設計依據。現行產品是 standalone-first；共享 `qml/` 不依賴 DMS，內建 light/dark palette 並可讀 generic matugen JSON。文中舊有 DMS token／prototype 描述只可視為視覺參考；DMS `Theme.*` 現在只由 optional compatibility wrapper 映射到最小 theme contract。

現行設計值（色彩、圓角、間距、字級、字型、透明度、陰影）全部集中在
`qml/LookupTheme.qml`，說明見 `docs/theming.md`；本手冊中的 `Theme.outlineMedium`、
Noto Serif 標題等片段已過時（outlineMedium 在 DMS 預設 outline 透明度 0 下不可見；
標題現為 Asebi Mincho，CJK 頭字為 Jigmo）。實作與生命週期真相以
`qml/LookupTheme.qml`、`qml/LookupCard.qml`、`qml/LookupController.qml` 及
`docs/json-schema.md` 為準，不以此手冊的歷史 prototype 片段為準。

## Sources checked

- Material 3 typography: Android Developers 記錄 M3 type scale 分成 `display`、`headline`、`title`、`body`、`label` 五組，每組 large/medium/small，常用尺寸包括 title/body/label 系列。
- Material 3 cards: M3 card pattern通常分 elevated / filled / outlined；card 用於承載與單一 subject 相關的內容和動作。
- Segmented buttons: segmented control 適合 2-4 個互斥選項，用於切換同一組內容的 view/filter。

Reference URLs:

- https://developer.android.com/develop/ui/compose/designsystems/material3
- https://developer.android.com/reference/kotlin/androidx/compose/material3/Typography
- https://m3.material.io/components/cards/overview
- https://m3.material.io/components/segmented-buttons/overview
- https://m3.material.io/components/tabs/overview

## Design goal

把 dankLookup popup 從「單一文字面板」提升為「小型百科/詞典卡片」。

目標效果：

```text
┌──────────────────────────────────────────────┐
│  consolidate                            ✕    │
│  /kәnˊsɒlәdeit/                              │
│                                              │
│  [漢語] [漢英] [英英]                         │
│                                              │
│  XDICT英漢辭典                                │
│  vt. 鞏固,使聯合,統一                         │
│  vi. 鞏固                                    │
│                                              │
│  fallback: FreeDict available                │
└──────────────────────────────────────────────┘
```

The first viewport should communicate:

- selected word / phrase
- pronunciation if available
- active dictionary mode
- primary definition
- source dictionary name

## Layout model

Use one card, not nested cards.

Recommended structure:

```text
FloatingWindow
  Rectangle card surface
    Column root
      Header row
        Query title
        Close icon button
      Optional pronunciation row
      Segmented dictionary switch
      Source label row
      Scrollable definition body
      Optional footer actions
```

Containment: the card has two visual zones separated by a single rule.

```text
┌──────────────────────────────────────────────┐
│  Query zone   (query title, pronunciation)   │
│──────────────────────────────────────────────│
│  Result zone  (segmented, source, body,      │
│                footer actions)               │
└──────────────────────────────────────────────┘
```

The query zone answers "what am I looking at"; the result zone answers "here is the answer". A thin horizontal rule (`0.5px`, `Theme.outlineMedium` or `var(--rule)`) at the bottom of the pronunciation row is sufficient. Do not add further dividers between segmented row, source label, and body — these are already visually distinct by element shape (pill buttons vs small label vs long text).

```qml
Rectangle {
    width: parent.width
    height: 1
    color: Theme.outlineMedium || "#444444"
    opacity: 0.5
}
```

Do not use background color blocks to separate zones within the card. The card itself is already a contained surface; internal zones rely on the rule plus spacing.

Avoid:

- card inside card
- large marketing-style hero
- oversized decorative background
- gradient/orb decoration
- multiline segmented labels
- hidden dictionary source

## Card style

Use a filled card as the default because this is a transient lookup surface, not a permanent document page.

Recommended tokens:

```qml
color: Theme.surfaceContainerHigh
border.color: Theme.outlineMedium
radius: Math.min(Theme.cornerRadius, 12)
```

If the DMS token is unavailable, fallback:

```qml
color: Theme.surfaceContainer || "#1f1f1f"
border.color: Theme.outlineMedium || "#444444"
radius: 12
```

Size:

```qml
minimumSize: Qt.size(360, 180)
implicitWidth: 520
implicitHeight: 320
```

Keep the popup compact. If content is long, scroll the body region instead of resizing the whole popup.

## Dictionary switching

Use segmented controls, not tabs, for `漢語 / 漢英 / 英英` because these are three views over the same selected query, not separate pages.

Recommended segments:

```text
漢語  → Chinese definition / Chinese-oriented explanation
漢英  → Chinese to English or English to Chinese depending query direction
英英  → English definition source
```

For current V2c data:

```text
漢語:
  Chinese query → 萌典重編國語辭典

漢英:
  English query → XDICT英漢辭典
  Chinese query → XDICT漢英辭典

英英:
  English query → dictd_www.dict.org_gcide
  fallback → WordNet
```

Implementation note:

- V2b may keep backend output as one `results[0]`.
- V2c uses `results_by_mode`, `available_modes`, and `default_mode`.
- Segment clicks must call `applyMode(modelData)` so the selected state, source label, and body update together.
- Disabled segments are acceptable when their backend result is missing.

Suggested QML state:

```qml
property string activeMode: "han-en"
property var availableModes: ["han", "han-en", "en-en"]
```

Labels should be short:

```text
漢語
漢英
英英
```

Do not use long labels like `Traditional Chinese Dictionary` inside the segmented row.

## Typography

Material 3 type roles should guide hierarchy:

- query title: `titleLarge` equivalent
- pronunciation: `bodyMedium`
- dictionary source: `labelMedium`
- definition body: `bodyLarge` or `bodyMedium`
- action labels: `labelLarge`

Local font decision:

- Query title for Chinese / Japanese: `Asebi Mincho`
- Query title for Latin: `Noto Serif`
- Body definition should stay sans-serif / DMS default for readability.

Local evidence:

```text
fc-match 'Asebi Mincho'
→ AsebiMin-Light.otf: "Asebi Mincho" "Light"

fc-match 'Noto Serif'
→ NotoSerif-Regular.ttf: "Noto Serif" "Regular"
```

Recommended QML:

```qml
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

Text {
    id: queryLabel
    text: root.queryText
    font.family: root.titleFontFamily()
    font.pixelSize: root.hasCjkOrKana(root.queryText) ? 24 : 23
    font.weight: Font.Normal
    lineHeight: 1.15
    color: Theme.surfaceText
    elide: Text.ElideRight
    maximumLineCount: 2
    wrapMode: Text.WordWrap
}
```

Do not use a CSS-style fallback list in `font.family`, such as:

```qml
font.family: "Asebi Mincho,Noto Serif CJK TC,Noto Serif"
```

Qt Quick expects a single resolved family name here; a comma-joined string can fail to match and fall back to the default sans font.

Definition body:

```qml
Text {
    text: root.resultBody
    font.pixelSize: 14
    lineHeight: 1.35
    color: Theme.surfaceText
    wrapMode: Text.WordWrap
    textFormat: Text.PlainText
}
```

Do not use viewport-width based font scaling.

## Encyclopedia-style details

The card should feel like a compact dictionary/encyclopedia entry:

- Query word is visually dominant.
- Pronunciation is directly below title when available.
- Source dictionary name is visible before the definition body.
- Definitions are grouped by source with bracket labels such as `[XDICT英漢辭典]`.
- Long fallback sources should appear after primary result, not before it.

For current backend body:

```text
[XDICT英漢辭典]
[kәnˊsɒlәdeit]
vt. 鞏固,使聯合,統一
vi. 鞏固
```

UI can parse the first bracket line as source label later, but V2b should not require parser changes unless needed.

## Interaction rules

Required:

- `Escape` closes popup.
- Close button uses a clear `✕` or DMS icon if available.
- Segmented row updates active mode without closing popup.
- If a mode has no data, show a compact empty state in the body region, not a modal.

Optional V2b actions:

- copy definition
- copy word
- open full dictionary source
- add to lookup history

Do not add network actions by default.

## Motion

The popup is transient — motion should be brief and purposeful, not decorative. Three scenarios only:

### Popup appear

Scale from 0.92 to 1.0 with opacity 0→1, ~200ms ease-out. The popup should feel like it surfaces toward the user, not slides in from an edge.

```qml
FloatingWindow {
    id: popup
    opacity: 0
    scale: 0.92

    NumberAnimation on opacity {
        to: 1; duration: 200
        easing.type: Easing.OutCubic
        running: popup.visible
    }
    NumberAnimation on scale {
        to: 1.0; duration: 200
        easing.type: Easing.OutCubic
        running: popup.visible
    }
}
```

### Popup dismiss

Opacity 1→0, ~150ms. No scale change on dismiss — it should vanish quickly without drawing attention back to itself.

```qml
function dismiss() {
    dismissAnim.start()
}
NumberAnimation {
    id: dismissAnim
    target: popup
    property: "opacity"
    to: 0; duration: 150
    easing.type: Easing.InCubic
    onFinished: popup.visible = false
}
```

### Mode switch cross-fade

When the user switches dictionary mode, the body content cross-fades: old content fades out (~100ms), new content fades in (~100ms). Do not animate the segmented row or source label — only the definition body region.

```qml
Behavior on bodyText {
    SequentialAnimation {
        NumberAnimation {
            target: bodyLabel
            property: "opacity"
            to: 0; duration: 100
            easing.type: Easing.InCubic
        }
        PropertyAction { target: bodyLabel; property: "text" }
        NumberAnimation {
            target: bodyLabel
            property: "opacity"
            to: 1; duration: 100
            easing.type: Easing.OutCubic
        }
    }
}
```

Avoid:

- spring / bounce animations — the popup is a utility surface, not a playful element
- position animations (slide-in from edge, drop from top)
- animating the card border, shadow, or radius
- motion longer than 250ms total for any single transition

## Current implementation boundary

This file is a visual-design reference, not a backend schema or implementation
plan. The shipped controller/card use schema 2 from `docs/json-schema.md`, show
all exact modes that produced useful results, keep Related separate, and render
plain text inside a bounded scrollable card.

The current appearance transition is 170 ms and does not gate content updates.
Loading shimmer transitions are 220 ms. Copy/Open act on the active exact,
related, or indexed token result. Missing dictionaries make a mode unavailable;
they do not crash or remove the standalone frontend.

Font availability is treated as an environment detail. The UI uses a safe
fallback stack rather than requiring a private font or a particular locally
deployed dictionary. Validate observable behavior with `tests/run_qml_smoke.py`
and latency with `tests/benchmark_ui.py`; do not revive the removed prototype
phases or their machine-specific acceptance claims.
