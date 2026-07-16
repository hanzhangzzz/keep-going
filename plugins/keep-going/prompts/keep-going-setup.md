---
description: Enable or disable the Keep Going Stop hook for the current Codex project
argument-hint: '[--enable|--disable] [--backend cli] [--command "..."] [--shell]'
allowed-tools: Bash(*)
---

Run exactly:

```bash
"__KEEP_GOING_PLUGIN_ROOT__/scripts/bridge.sh" setup --project "$PWD" --host codex $ARGUMENTS
```

Output the final bridge state exactly as printed by the command.

Examples:
- `keep-going-setup --enable`
- `keep-going-setup --disable`
- `keep-going-setup --enable --host codex`
- `keep-going-setup --enable --backend cli --command "omxm" --shell`
