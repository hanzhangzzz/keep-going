# Keep Going local CLI wrapper

The `keep-going` package is not published to the npm registry. Do not use
`npx keep-going` or `npm install keep-going`; those registry paths return 404.
This directory is a local wrapper used from a cloned Keep Going checkout and
for package build checks. The repository's supported user entrypoint is the
Python CLI described in the root README.

From the repository root, use the source CLI:

```sh
uv sync
uv run keep-going onboard --project /path/to/your-project --host auto
uv run keep-going status --project /path/to/your-project
```

For local wrapper development, run the checked-in script directly:

```sh
node packages/npm/bin/keep-going.js --help
node packages/npm/bin/keep-going.js install --source . --dry-run
node packages/npm/bin/keep-going.js sync-local --source "$PWD" --no-register-hosts
```

Requires Node.js 18+, Python 3.11+, `uv`, and an authenticated Codex or
Claude Code CLI when running model-backed commands.

`onboard` is the default new-user path. It selects a bounded, scrubbed sample from the chosen host's recent local sessions, distills personal decision DNA through that authenticated CLI, persists canonical and runtime policies in a version-independent local user directory, installs the integration, enables the current project, and verifies the Stop hook.

The local runtime bundle contains only the public decision-policy template. It never contains a maintainer's canonical policy, runtime policy, session data, local configuration, or host paths. Personal DNA is created only on the user's machine.

Lifecycle commands remain available:

```sh
node packages/npm/bin/keep-going.js sync-local --source "$PWD" --no-register-hosts
node packages/npm/bin/keep-going.js install --source "$PWD" --no-register-hosts
node packages/npm/bin/keep-going.js upgrade --source "$PWD" --no-register-hosts
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
Current Codex CLI releases do not dispatch plugin or user-installed custom slash commands in the TUI; use `$keep-going`, MCP, or the local wrapper commands instead:

```sh
node packages/npm/bin/keep-going.js start --source "$PWD" --project /path/to/your-project --no-register-hosts
node packages/npm/bin/keep-going.js enable --project /path/to/your-project --host codex
node packages/npm/bin/keep-going.js status --project /path/to/your-project
node packages/npm/bin/keep-going.js disable --project /path/to/your-project
```

The wrapper copies the Keep Going Python runtime into `~/.keep-going/runtime/<version>` and then runs the repo-local `uv run keep-going ...` commands from that stable runtime path. Use `upgrade` to replace an existing install, or `install --force` when you intentionally want reinstall semantics.

## License

No license is granted. This package is source-visible for review; reuse,
modification, and redistribution require permission from the copyright holder.
