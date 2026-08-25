---
description: Distill local sessions into personal DNA, deploy Keep Going, and verify it
argument-hint: '[--max-sessions N] [--scope recent|project] [--replace]'
allowed-tools: Bash(*)
---

Run:

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/onboard.sh" --project "${CLAUDE_PROJECT_DIR:-$PWD}" --host claude-code $ARGUMENTS
```

Explain that a scrubbed, bounded session sample is sent to the user's authenticated Claude Code CLI. Then return the profile summary, selected sample count, local artifact paths, deployment status, and suggested first trial exactly as printed.
