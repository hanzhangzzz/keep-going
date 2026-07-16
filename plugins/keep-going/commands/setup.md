---
description: Enable or disable the Keep Going Stop hook for the current project
argument-hint: '[--enable|--disable] [--backend cli] [--command "..."] [--shell] [--host claude-code|codex]'
allowed-tools: Bash(*)
---

Run:

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/bridge.sh" setup --project "${CLAUDE_PROJECT_DIR:-$PWD}" $ARGUMENTS
```

Examples:
- `/keep-going:setup --enable`
- `/keep-going:setup --disable`
- `/keep-going:setup --enable --host codex`
- `/keep-going:setup --enable --backend cli --command "c 0" --shell`
- `/keep-going:setup --enable --backend cli --command "omxm" --shell`

Output the final bridge state exactly as printed by the command.
