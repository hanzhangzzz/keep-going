# Keep Going npm CLI

Install or upgrade Keep Going integration surfaces from npm:

```sh
npx keep-going sync-local
npx keep-going install
npx keep-going upgrade
```

The published package contains only the public decision-policy template. It never contains a maintainer's canonical policy, runtime policy, session data, local configuration, or host paths. Create and compile a private policy in the installed runtime before using `start`:

```sh
runtime="$(npx keep-going runtime-path)"
cp "$runtime/artifacts/decision-policy.template.yaml" "$runtime/artifacts/decision-policy.yaml"
(cd "$runtime" && uv run keep-going compile-policy)
npx keep-going start
```

`start` installs or refreshes the runtime, registers detected host plugins, installs the Codex native Stop hook, enables the current project with `--host codex`, and runs verification. It fails explicitly when persisted private decision policy has not been initialized.

`sync-local` is the one-command path for local development: it refreshes `~/.keep-going/runtime/<version>`, installed skill/plugin/hook files, the Codex native Stop hook, and then runs install verification.

`install` registers detected host plugins by default, using official host commands instead of editing private registries directly:

```sh
claude plugin marketplace add <runtime>
claude plugin install keep-going@keep-going-local
codex plugin marketplace add <runtime>
codex plugin add keep-going@keep-going-local
```

Use `--register-hosts claude-code|codex|all|auto|none` or `--no-register-hosts` to control registration.

Claude Code exposes the plugin commands as slash commands, for example `/keep-going:setup --enable`.
Current Codex CLI releases do not dispatch plugin or user-installed custom slash commands in the TUI; use `$keep-going`, MCP, or the npm control commands instead:

```sh
npx keep-going start --project "$PWD"
npx keep-going enable --project "$PWD" --host codex
npx keep-going status --project "$PWD"
npx keep-going disable --project "$PWD"
```

For local development, point the wrapper at a checkout:

```sh
node packages/npm/bin/keep-going.js install --source . --dry-run
node packages/npm/bin/keep-going.js sync-local --source . --no-register-hosts
```

The wrapper copies the Keep Going Python runtime into `~/.keep-going/runtime/<version>` and then runs the repo-local `uv run keep-going ...` commands from that stable runtime path. Use `upgrade` to replace an existing install, or `install --force` when you intentionally want reinstall semantics.

## License

No license is granted. This package is source-visible for review; reuse,
modification, and redistribution require permission from the copyright holder.
