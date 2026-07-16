---
description: Manage the Keep Going Stop hook for the current Codex project
argument-hint: 'setup|enable|disable|status|self-test [options]'
allowed-tools: Bash(*)
---

Run exactly:

```bash
set -- $ARGUMENTS
case "${1:-status}" in
  setup)
    shift
    "__KEEP_GOING_PLUGIN_ROOT__/scripts/bridge.sh" setup --project "$PWD" --host codex "$@"
    ;;
  enable)
    shift
    "__KEEP_GOING_PLUGIN_ROOT__/scripts/bridge.sh" setup --project "$PWD" --host codex --enable "$@"
    ;;
  disable)
    shift
    "__KEEP_GOING_PLUGIN_ROOT__/scripts/bridge.sh" setup --project "$PWD" --disable "$@"
    ;;
  status)
    shift
    "__KEEP_GOING_PLUGIN_ROOT__/scripts/bridge.sh" status --project "$PWD" "$@"
    ;;
  self-test)
    shift
    "__KEEP_GOING_PLUGIN_ROOT__/scripts/bridge.sh" self-test --project "$PWD" "$@"
    ;;
  *)
    echo "usage: keep-going setup|enable|disable|status|self-test [options]" >&2
    exit 2
    ;;
esac
```

Examples:
- `keep-going setup --enable`
- `keep-going enable`
- `keep-going disable`
- `keep-going status`
- `keep-going self-test`
