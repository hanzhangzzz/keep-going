#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
: "${ANTHROPIC_API_KEY:?需要先 export ANTHROPIC_API_KEY}"
uv run keep-going reason "$@"
