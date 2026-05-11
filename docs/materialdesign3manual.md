# dankLookup Material Design 3 manual

用途：給 opencode 作為 dankLookup V2b UI 改造手冊。這份手冊不是要做 landing page，而是把現有 DMS popup 做成更接近 Material Design 3 的查詞卡片：清楚、可掃讀、可切換詞典來源，且保留 Dank Material Shell / matugen 的既有視覺語言。

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

## Backend contract for future UI

Current contract:

```json
{
  "backend": "sdcv",
  "title": "consolidate",
  "subtitle": "XDICT英漢辭典",
  "body": "[XDICT英漢辭典]\n..."
}
```

Recommended V2b-compatible extension:

```json
{
  "backend": "sdcv",
  "title": "consolidate",
  "subtitle": "XDICT英漢辭典",
  "body": "[XDICT英漢辭典]\n...",
  "mode": "han-en",
  "sources": ["XDICT英漢辭典"],
  "available_modes": ["han-en"]
}
```

Do not break V2a QML. Add fields only; keep `title` / `subtitle` / `body`.

## Implementation plan for opencode

Phase UI-1: QML-only visual update

- Keep backend unchanged.
- Replace plain popup layout with header + segmented row + source label + scroll body.
- Use `Asebi Mincho` for query title only.
- Keep body as `Text.PlainText`.
- Keep popup size compact.

Phase UI-2: Mode state

- Add `activeMode`.
- Add disabled-looking segments for missing modes.
- Current backend result maps to `漢語`, `漢英`, or `英英` depending on query classification and available dictionary hits.

Phase UI-3: Backend multi-mode

- Extend backend JSON with `default_mode`, `available_modes`, and `results_by_mode`.
- `en-en` is available through GCIDE with WordNet fallback.
- `han` is available through 萌典重編國語辭典 for Chinese entries.

## Acceptance checklist

- Popup remains floating and centered under niri.
- No repeated popup.
- No QML `checkForUpdate error`.
- Query title uses `Asebi Mincho` for CJK / Japanese and `Noto Serif` for Latin.
- Body remains readable at 14px with scroll.
- Segmented row does not overflow at `implicitWidth: 520`.
- English `consolidate` shows XDICT first.
- English `consolidate` can switch to `英英` and show GCIDE/WordNet content.
- Chinese `字典` / `正常` can switch between available `漢語` and `漢英` results.
- Missing mode does not crash UI.
