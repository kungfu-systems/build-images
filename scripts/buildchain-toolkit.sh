#!/bin/bash
set -euo pipefail

if ! command -v pnpm >/dev/null 2>&1; then
  echo "pnpm is required to run the Buildchain toolkit" >&2
  exit 1
fi

exec pnpm exec buildchain "$@"
