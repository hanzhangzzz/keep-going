#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

if [[ -x "$repo_root/.venv/bin/keep-going" ]]; then
  exec "$repo_root/.venv/bin/keep-going" mcp
fi

exec uv run keep-going mcp
