#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd "$(dirname "$0")" && pwd)"
candidate_root="$(cd "$script_dir/../../.." && pwd)"
if [[ -x "$candidate_root/scripts/04-mcp.sh" ]]; then
  repo_root="$candidate_root"
elif [[ -n "${KEEP_GOING_REPO_ROOT:-}" && -x "$KEEP_GOING_REPO_ROOT/scripts/04-mcp.sh" ]]; then
  repo_root="$KEEP_GOING_REPO_ROOT"
elif [[ -n "${KEEP_GOING_RUNTIME_ROOT:-}" && -x "$KEEP_GOING_RUNTIME_ROOT/scripts/04-mcp.sh" ]]; then
  repo_root="$KEEP_GOING_RUNTIME_ROOT"
elif [[ -f "$script_dir/../runtime-root" ]]; then
  repo_root="$(cat "$script_dir/../runtime-root")"
elif [[ -f "$script_dir/../.repo-root" ]]; then
  repo_root="$(cat "$script_dir/../.repo-root")"
else
  echo "cannot resolve Keep Going repo root" >&2
  exit 1
fi
if [[ ! -x "$repo_root/scripts/04-mcp.sh" ]]; then
  echo "Keep Going MCP wrapper missing or not executable: $repo_root/scripts/04-mcp.sh" >&2
  exit 1
fi
if [[ -z "${KEEP_GOING_USER_HOME:-}" && -f "$script_dir/../user-home" ]]; then
  export KEEP_GOING_USER_HOME="$(cat "$script_dir/../user-home")"
fi
exec "$repo_root/scripts/04-mcp.sh" "$@"
