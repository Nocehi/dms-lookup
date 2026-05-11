---
name: pre-check
description: >-
  Use before writing any QML or backend code. Runs Quickshell API survey
  and confirms spec/proposal exists for the change.
---
## Before writing code

1. Confirm a spec or proposal exists in ~/文件/DankLookup/ for this change
2. For any new QML type usage, survey available API:

!`rg -l "qmltypes" /usr/lib/qt6/qml/Quickshell/ 2>/dev/null | head -5`

3. Check the property/signal/method you plan to use actually exists:
   rg -A2 "name: \"<Type>\"" /usr/lib/qt6/qml/Quickshell/Io/quickshell-io.qmltypes

4. Cross-check against qs-compat skill for known traps before proceeding

