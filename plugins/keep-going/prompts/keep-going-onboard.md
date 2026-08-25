---
description: Distill local sessions into personal DNA and deploy Keep Going for this Codex project
argument-hint: '[--max-sessions N] [--scope recent|project] [--replace]'
allowed-tools: Bash(*)
---

Run exactly:

```bash
"__KEEP_GOING_PLUGIN_ROOT__/scripts/onboard.sh" --project "$PWD" --host codex $ARGUMENTS
```

Tell the user that only a scrubbed, bounded sample is sent through their authenticated Codex CLI. Return the resulting profile, evidence count, artifact paths, deployment state, and first trial.
