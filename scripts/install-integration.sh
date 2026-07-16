#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
codex_home="${CODEX_HOME:-$HOME/.codex}"
agents_home="${AGENTS_HOME:-$HOME/.agents}"
claude_home="${CLAUDE_HOME:-$HOME/.claude}"
codex_home_set=0
agents_home_set=0
claude_home_set=0
execute=0
force=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --execute)
      execute=1
      shift
      ;;
    --dry-run)
      execute=0
      shift
      ;;
    --force|--upgrade)
      force=1
      shift
      ;;
    --codex-home)
      codex_home="${2:?missing value for --codex-home}"
      codex_home_set=1
      shift 2
      ;;
    --agents-home)
      agents_home="${2:?missing value for --agents-home}"
      agents_home_set=1
      shift 2
      ;;
    --claude-home)
      claude_home="${2:?missing value for --claude-home}"
      claude_home_set=1
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ "$codex_home_set" -eq 1 && "$agents_home_set" -eq 0 ]]; then
  agents_home="$codex_home/.agents"
fi

skill_src="$repo_root/.codex/skills/keep-going"
skill_dst="$codex_home/skills/keep-going"
agent_src="$repo_root/.codex/agents/keep-going.toml"
agent_dst="$codex_home/agents/keep-going.toml"
plugin_src="$repo_root/plugins/keep-going"
plugin_dst="$agents_home/plugins/keep-going"
codex_prompt_dst="$codex_home/prompts"
codex_command_dst="$codex_home/commands"
marketplace_src="$repo_root/.agents/plugins/marketplace.json"
marketplace_dst="$agents_home/plugins/marketplace.json"
claude_marketplace_src="$repo_root/.claude-plugin/marketplace.json"
claude_marketplace_dst="$claude_home/plugins/marketplaces/keep-going-local/.claude-plugin/marketplace.json"
claude_plugin_dst="$claude_home/plugins/marketplaces/keep-going-local/plugins/keep-going"

if [[ ! -f "$skill_src/SKILL.md" ]]; then
  echo "missing skill source: $skill_src/SKILL.md" >&2
  exit 1
fi
if [[ ! -f "$agent_src" ]]; then
  echo "missing agent source: $agent_src" >&2
  exit 1
fi
if [[ ! -f "$plugin_src/.codex-plugin/plugin.json" ]]; then
  echo "missing plugin manifest: $plugin_src/.codex-plugin/plugin.json" >&2
  exit 1
fi
if [[ ! -f "$plugin_src/.claude-plugin/plugin.json" ]]; then
  echo "missing Claude plugin manifest: $plugin_src/.claude-plugin/plugin.json" >&2
  exit 1
fi
if [[ ! -f "$marketplace_src" ]]; then
  echo "missing marketplace entry: $marketplace_src" >&2
  exit 1
fi
if [[ ! -f "$claude_marketplace_src" ]]; then
  echo "missing Claude marketplace entry: $claude_marketplace_src" >&2
  exit 1
fi

cat <<EOF
Keep Going integration plan
- repo: $repo_root
- codex_home: $codex_home
- agents_home: $agents_home
- claude_home: $claude_home
- codex_legacy_skill_cleanup: $skill_dst
- codex_legacy_prompt_cleanup: $codex_prompt_dst/keep-going*.md
- codex_legacy_command_cleanup: $codex_command_dst/keep-going*.md
- codex_slash_commands: unsupported by current Codex CLI plugin surface; use \$keep-going, MCP, or 'keep-going enable/status/disable --project <project>'
- agent: $agent_src -> $agent_dst
- codex_plugin: $plugin_src -> $plugin_dst
- codex_marketplace_source: $marketplace_src
- codex_marketplace_registration: handled by host CLI via 'codex plugin marketplace add'
- codex_legacy_marketplace_cleanup: $marketplace_dst
- claude_marketplace: $claude_marketplace_src -> $claude_marketplace_dst
- claude_plugin: $plugin_src -> $claude_plugin_dst
- mcp_command: $repo_root/scripts/04-mcp.sh
- bridge_enable: cd "$repo_root" && uv run keep-going bridge enable --project <project>
- force: $force

MCP config snippet:
{
  "mcpServers": {
    "keep-going": {
      "command": "$repo_root/scripts/04-mcp.sh"
    }
  }
}
EOF

if [[ "$execute" -eq 0 ]]; then
  echo
  echo "dry-run only; pass --execute to install integration files and register host marketplaces."
  exit 0
fi

refuse_or_replace() {
  local path="$1"
  if [[ ! -e "$path" ]]; then
    return
  fi
  if [[ "$force" -ne 1 ]]; then
    echo "target already exists, refusing to overwrite: $path" >&2
    echo "pass --force to replace installed Keep Going integration files" >&2
    exit 1
  fi
  rm -rf "$path"
}

refuse_or_replace "$agent_dst"
refuse_or_replace "$plugin_dst"
refuse_or_replace "$claude_marketplace_dst"
refuse_or_replace "$claude_plugin_dst"

cleanup_legacy_codex_skill() {
  local path="$1"
  local skill_file="$path/SKILL.md"
  if [[ ! -e "$path" ]]; then
    echo "legacy Codex skill absent: $path"
    return
  fi
  if [[ -f "$skill_file" ]] && grep -Eq 'Keep Going|keep-going|scripts/03-reply\.sh|keep-going reply' "$skill_file"; then
    rm -rf "$path"
    echo "removed legacy Codex skill: $path"
    return
  fi
  echo "kept non-Keep Going Codex skill: $path"
}

cleanup_legacy_codex_file() {
  local path="$1"
  local kind="$2"
  if [[ ! -e "$path" ]]; then
    return
  fi
  if [[ -f "$path" ]] && grep -Eq 'Keep Going|keep-going|KEEP_GOING_PLUGIN_ROOT|scripts/bridge\.sh|keep-going bridge' "$path"; then
    rm -f "$path"
    echo "removed legacy Codex $kind: $path"
    return
  fi
  echo "kept non-Keep Going Codex $kind: $path"
}

cleanup_legacy_codex_marketplace() {
  local path="$1"
  if [[ ! -f "$path" ]]; then
    return
  fi
  if grep -Eq '"name"[[:space:]]*:[[:space:]]*"keep-going-local"' "$path"; then
    rm -f "$path"
    echo "removed legacy Codex marketplace shadow: $path"
  else
    echo "kept non-Keep Going Codex marketplace: $path"
  fi
}

write_runtime_markers() {
  local plugin_root="$1"
  mkdir -p "$plugin_root"
  printf '%s\n' "$repo_root" > "$plugin_root/runtime-root"
  printf '%s\n' "$repo_root" > "$plugin_root/.repo-root"
}

write_source_runtime_marker() {
  if [[ "$codex_home_set" -eq 0 && "$agents_home_set" -eq 0 && "$claude_home_set" -eq 0 ]]; then
    printf '%s\n' "$repo_root" > "$plugin_src/runtime-root"
  fi
}

cleanup_legacy_codex_skill "$skill_dst"
cleanup_legacy_codex_file "$codex_prompt_dst/keep-going.md" "prompt"
cleanup_legacy_codex_file "$codex_prompt_dst/keep-going-setup.md" "prompt"
cleanup_legacy_codex_file "$codex_prompt_dst/keep-going-status.md" "prompt"
cleanup_legacy_codex_file "$codex_prompt_dst/keep-going-self-test.md" "prompt"
cleanup_legacy_codex_file "$codex_prompt_dst/keep-going:setup.md" "prompt"
cleanup_legacy_codex_file "$codex_prompt_dst/keep-going:status.md" "prompt"
cleanup_legacy_codex_file "$codex_prompt_dst/keep-going:self-test.md" "prompt"
cleanup_legacy_codex_file "$codex_command_dst/keep-going.md" "command"
cleanup_legacy_codex_file "$codex_command_dst/keep-going-setup.md" "command"
cleanup_legacy_codex_file "$codex_command_dst/keep-going-status.md" "command"
cleanup_legacy_codex_file "$codex_command_dst/keep-going-self-test.md" "command"
cleanup_legacy_codex_file "$codex_command_dst/keep-going:setup.md" "command"
cleanup_legacy_codex_file "$codex_command_dst/keep-going:status.md" "command"
cleanup_legacy_codex_file "$codex_command_dst/keep-going:self-test.md" "command"
echo "codex slash commands unsupported by current Codex CLI plugin surface"
mkdir -p "$(dirname "$agent_dst")"
cp "$agent_src" "$agent_dst"
echo "installed agent: $agent_dst"
mkdir -p "$(dirname "$plugin_dst")"
cp -R "$plugin_src" "$plugin_dst"
write_runtime_markers "$plugin_dst"
echo "installed plugin: $plugin_dst"
cleanup_legacy_codex_marketplace "$marketplace_dst"
echo "codex marketplace registration: use host CLI with $marketplace_src"
write_source_runtime_marker
mkdir -p "$(dirname "$claude_marketplace_dst")"
cp "$claude_marketplace_src" "$claude_marketplace_dst"
echo "installed Claude marketplace: $claude_marketplace_dst"
mkdir -p "$(dirname "$claude_plugin_dst")"
cp -R "$plugin_src" "$claude_plugin_dst"
write_runtime_markers "$claude_plugin_dst"
echo "installed Claude plugin: $claude_plugin_dst"
