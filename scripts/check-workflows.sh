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
verify_workflow="$repo_root/.github/workflows/verify.yml"
publisher="$repo_root/scripts/publish-image-family.sh"
if ! grep -Fq 'release-passport-impact-json: ".buildchain/release-impact.json"' "$promotion_workflow"; then
  echo "Buildchain promotion must supply the production release passport impact ledger" >&2
  exit 1
fi

if ! grep -Fq 'packages: write' "$promotion_workflow"; then
  echo "Buildchain promotion must retain GHCR package write permission" >&2
  exit 1
fi

if grep -Fq 'packages: write' "$verify_workflow"; then
  echo "Normal verification must not receive GHCR package write permission" >&2
  exit 1
fi

# shellcheck disable=SC2016
if ! grep -Fq 'BUILDCHAIN_REGISTRY_CACHE_WRITE="${BUILDCHAIN_REGISTRY_CACHE_WRITE:-true}"' "$publisher"; then
  echo "Only the Buildchain lifecycle publisher may enable registry cache writes" >&2
  exit 1
fi
