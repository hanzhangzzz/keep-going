#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd "$(dirname "$0")" && pwd)"
candidate_root="$(cd "$script_dir/../../.." && pwd)"
if [[ -f "$candidate_root/pyproject.toml" && -d "$candidate_root/src/keep_going" ]]; then
  runtime_root="$candidate_root"
elif [[ -n "${KEEP_GOING_RUNTIME_ROOT:-}" && -f "$KEEP_GOING_RUNTIME_ROOT/pyproject.toml" ]]; then
  runtime_root="$KEEP_GOING_RUNTIME_ROOT"
elif [[ -n "${KEEP_GOING_REPO_ROOT:-}" && -f "$KEEP_GOING_REPO_ROOT/pyproject.toml" ]]; then
  runtime_root="$KEEP_GOING_REPO_ROOT"
elif [[ -f "$script_dir/../runtime-root" ]]; then
  runtime_root="$(cat "$script_dir/../runtime-root")"
elif [[ -f "$script_dir/../.repo-root" ]]; then
  runtime_root="$(cat "$script_dir/../.repo-root")"
else
  echo "cannot resolve Keep Going runtime root" >&2
  exit 1
fi
if [[ ! -f "$runtime_root/pyproject.toml" || ! -d "$runtime_root/src/keep_going" ]]; then
  echo "Keep Going runtime root is invalid: $runtime_root" >&2
  exit 1
fi
if [[ -z "${KEEP_GOING_USER_HOME:-}" && -f "$script_dir/../user-home" ]]; then
  export KEEP_GOING_USER_HOME="$(cat "$script_dir/../user-home")"
fi
cd "$runtime_root"
exec uv run keep-going onboard "$@"
