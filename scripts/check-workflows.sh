#!/bin/bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if command -v actionlint >/dev/null 2>&1; then
  # Keep this narrow so hidden reusable workflows are covered without depending
  # on shell glob behavior.
  mapfile_output="$(find "$repo_root/.github/workflows" -type f -name '*.yml' -print | sort)"
  if [ -n "$mapfile_output" ]; then
    # shellcheck disable=SC2086
    actionlint -color=false $mapfile_output
  fi
else
  echo "actionlint not found; skipping workflow lint"
fi

promotion_workflow="$repo_root/.github/workflows/buildchain-ref-promotion.yml"
if ! grep -Fq 'release-passport-impact-json: ".buildchain/release-impact.json"' "$promotion_workflow"; then
  echo "Buildchain promotion must supply the production release passport impact ledger" >&2
  exit 1
fi
