#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
uv run keep-going reply "$@"
