---
description: Run the Keep Going bridge self-test
argument-hint: ''
disable-model-invocation: true
allowed-tools: Bash(*)
---

!`"${CLAUDE_PLUGIN_ROOT}/scripts/bridge.sh" self-test --project "${CLAUDE_PROJECT_DIR:-$PWD}"`
