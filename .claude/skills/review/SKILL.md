---
name: review
description: >-
  Use when asked to review the dank-lookup codebase. Read-only: do not
  modify any files. Provide balanced assessment considering project
  scope and audience.
---
## Review constraints
- Do NOT modify any files
- Do NOT apply enterprise standards to this project
- Consider: this is a local CLI tool + DMS plugin for personal use,
  potentially heading to GitHub as a small open-source project

## Review structure
Follow REVIEW_TEMPLATE.md in this directory for output format.

## Context
- Architecture: three-stage pipeline (shell selection → Python backend → QML UI)
- Full spec in AGENTS.md at repo root
- Development history in ~/文件/DankLookup/
- 863 lines total across 3 main files
