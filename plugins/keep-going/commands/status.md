---
description: Show Keep Going Stop hook state for the current project
argument-hint: ''
disable-model-invocation: true
allowed-tools: Bash(*)
---

!`"${CLAUDE_PLUGIN_ROOT}/scripts/bridge.sh" status --project "${CLAUDE_PROJECT_DIR:-$PWD}"`
