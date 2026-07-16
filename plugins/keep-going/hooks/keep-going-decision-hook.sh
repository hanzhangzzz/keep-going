#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd "$(dirname "$0")" && pwd)"
candidate_root="$(cd "$script_dir/../../.." && pwd)"
if [[ -f "$candidate_root/pyproject.toml" && -d "$candidate_root/src/keep_going" ]]; then
  repo_root="$candidate_root"
elif [[ -n "${KEEP_GOING_REPO_ROOT:-}" && -f "$KEEP_GOING_REPO_ROOT/pyproject.toml" && -d "$KEEP_GOING_REPO_ROOT/src/keep_going" ]]; then
  repo_root="$KEEP_GOING_REPO_ROOT"
elif [[ -n "${KEEP_GOING_RUNTIME_ROOT:-}" && -f "$KEEP_GOING_RUNTIME_ROOT/pyproject.toml" && -d "$KEEP_GOING_RUNTIME_ROOT/src/keep_going" ]]; then
  repo_root="$KEEP_GOING_RUNTIME_ROOT"
elif [[ -f "$script_dir/../runtime-root" ]]; then
  repo_root="$(cat "$script_dir/../runtime-root")"
elif [[ -f "$script_dir/../.repo-root" ]]; then
  repo_root="$(cat "$script_dir/../.repo-root")"
else
  echo "cannot resolve Keep Going repo root" >&2
  exit 1
fi
if [[ ! -f "$repo_root/pyproject.toml" || ! -d "$repo_root/src/keep_going" ]]; then
  echo "Keep Going repo root is invalid: $repo_root" >&2
  exit 1
fi
cd "$repo_root"
exec uv run keep-going hook --input-json
