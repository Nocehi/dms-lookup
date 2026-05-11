# notes/

開發筆記、proposal、版本 handoff、post-mortem。**這個目錄不是給專案使用者讀的**，是給 dankLookup 的開發者（本人 + AI 協作者，如 Claude / opencode / codex）作為跨 session 工作記憶用的。

使用者文件在 `../docs/`。版本歷史條目在 `../CHANGELOG.md`。

## 如何使用本索引

每條索引包含：版本錨點 / 範圍 / 狀態。狀態語意：

- **active** — 仍在指引當前 / 未來決策，打開有用
- **historical** — 描述已落地且不再變動的特性，僅作脈絡參考；具體實作以程式碼為準
- **partial** — 提案僅部分落地，剩餘工作未排程
- **concept** — 概念，尚未實作

## 索引

### 跨版本 / 方向性

| 檔案 | 範圍 | 狀態 |
|------|------|------|
| [github-release-roadmap.md](github-release-roadmap.md) | 從本機原型走向公開 GitHub repo 的待辦路線圖（工程、授權、隱私、安裝、維運）| active |
| [big-bang-concept.md](big-bang-concept.md) | Smartisan Big Bang 風格多詞 CJK 選取分詞 fallback 概念 | concept |

### v0.4.x — 結果品質、模式路由、測試基礎建設

| 檔案 | 範圍 | 狀態 |
|------|------|------|
| [v0.4.6-pill-actions.md](v0.4.6-pill-actions.md) | 單模式 pill + Copy/Open-full 動作設計（problem + design） | historical |
| [v0.4.6-implementation-handoff.md](v0.4.6-implementation-handoff.md) | v0.4.6 對 opencode 的 QML 實作交接（具體 diff 範本） | historical |
| [v0.4.7b-test-infrastructure-cleanup.md](v0.4.7b-test-infrastructure-cleanup.md) | `DANK_LOOKUP_OUTPUT` per-test 隔離；ja/yue fixture set | historical |
| [v0.4.8-mode-routing-and-source-quality.md](v0.4.8-mode-routing-and-source-quality.md) | CJK 多單語源並存時的 default mode 選擇政策；reading-routing fixture 確認 | active（政策仍指導未來路由決策） |
| [v0.4.9-risk-hardening.md](v0.4.9-risk-hardening.md) | classify() unknown fallback、字典 redistribution 標籤、CI-minimal config、pre-release-check | historical |

### v0.5.0 — QML 重構 / API 修正

| 檔案 | 範圍 | 狀態 |
|------|------|------|
| [v0.5.0-qml-button-refactor-proposal.md](v0.5.0-qml-button-refactor-proposal.md) | state-per-control + T.Templates 全 QML 重構提案 | **partial**（a 落地 3 個按鈕；segmented row、Related toggle、typescale 未做） |
| [v0.5.0a-qml-button-refactor-handoff.md](v0.5.0a-qml-button-refactor-handoff.md) | close / Copy / Open-full 三按鈕 T.Button 遷移（小可逆切片） | historical |
| [v0.5.0b-copy-action-fix.md](v0.5.0b-copy-action-fix.md) | `Qt.application.clipboard` 在 DMS runtime 不存在 → `wl-copy` via `Quickshell.Io.Process` | historical |
| [v0.5.0c-review-prep-and-doc-sync-handoff.md](v0.5.0c-review-prep-and-doc-sync-handoff.md) | review 前 doc sync：對齊 v0.5.0a 與實際落地差異（helper component 棄置、Copy 改 wl-copy） | historical |
| [v0.5.0d.qml-process-lifecycle-fix.md](v0.5.0d.qml-process-lifecycle-fix.md) | `Process.stdinEnabled = false` 關閉 stdin 傳遞 EOF；Quickshell API 調查紀錄 | historical |

### 觸摸板手勢探索

| 檔案 | 範圍 | 狀態 |
|------|------|------|
| [gesture-trigger-exploration.md](gesture-trigger-exploration.md) | fusuma 評估、4-finger tap watcher 方案選型、環境探測結果 | active（個人試用中，待 2 週體感數據） |

### future/ — 遠期參考模式

不在路線圖上。從外部專案觀察到的設計模式，當規模需求出現時再評估。

| 檔案 | 來源 | 摘要 |
|------|------|------|
| [future/per-mode-config-fallback.md](future/per-mode-config-fallback.md) | fusuma config.rb | 三層 config lookup（gesture-specific > global > default）；DankLookup 5 mode 扁平 dict 目前夠用 |
| [future/per-app-context.md](future/per-app-context.md) | fusuma-plugin-appmatcher | Per-app 字典模式切換（Obsidian 走漢英、Bilibili 走日語）；需 niri IPC，待場景驗證 |

## 相依檔位於 ~/文件/DankLookup/

依 `danklookup-dev` skill 的目錄分工：repo `notes/` 是 stage 過的子集，個人 `~/文件/DankLookup/` 是 master（含 AI session 轉錄、review 巨檔、早期版本筆記，不適合公開）。下列檔在現存筆記的 `depends_on:` frontmatter 中被引用，未複製進 repo，但**在個人目錄內存在**：

| 引用名稱 | 個人目錄位置 | 大小 / 建立時間 |
|----------|-------------|-----------------|
| v0.4.3-result-quality-policy.md | `~/文件/DankLookup/v0.4.3-result-quality-policy.md` | 12 KB, 2026-05-06 |
| v0.4.4-file-based-source-config.md | `~/文件/DankLookup/v0.4.4-file-based-source-config.md` | 13 KB, 2026-05-06 |
| v0.4.7-ja-yue-dictionary-deployment-handoff.md | `~/文件/DankLookup/v0.4.7-ja-yue-dictionary-deployment-handoff.md` | 12 KB, 2026-05-07 |
| v2d-ui-polish-manual.md | `~/文件/DankLookup/v2d-ui-polish-manual.md` | 8 KB, 2026-05-06 |
| v2f-source-specific-text-cleanup.md | `~/文件/DankLookup/v2f-source-specific-text-cleanup.md` | 16 KB, 2026-05-06 |

未來若 AI session 順著 `depends_on:` 鏈追上下文，到個人目錄取 master copy；不需要 stage 進 repo 除非該檔涉及未來公開發佈所需脈絡。

## 寫新筆記時

- frontmatter 用既有格式：`title / status / target / scope / created / depends_on`
- `status` 取值：`planned` / `concept` / `proposal` / `handoff` / `implemented-local` / `research-notes`
- 新增條目時順手更新本索引；過時項目改狀態而不是刪除
