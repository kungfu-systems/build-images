#!/bin/bash
set -euo pipefail

if ! command -v npm >/dev/null 2>&1; then
  echo "npm is required to run the Buildchain toolkit" >&2
  exit 1
fi

exec npm exec --yes --package "@kungfu-tech/buildchain@^3.0.0" -- buildchain "$@"
