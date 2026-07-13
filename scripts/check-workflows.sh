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
if ! grep -Fq "uses: kungfu-systems/buildchain/actions/promote-buildchain-ref@v2-alpha" "$promotion_workflow" ||
   ! grep -Fq "uses: kungfu-systems/buildchain/actions/promote-buildchain-ref@v2" "$promotion_workflow"; then
  echo "Buildchain promotion must route alpha and stable channels to their matching action refs" >&2
  exit 1
fi
# shellcheck disable=SC2016
if ! grep -Fq "if: \${{ startsWith(steps.target_ref.outputs.target_ref, 'alpha/') }}" "$promotion_workflow" ||
   ! grep -Fq "if: \${{ !startsWith(steps.target_ref.outputs.target_ref, 'alpha/') }}" "$promotion_workflow"; then
  echo "Buildchain promotion action refs must be selected from the resolved target channel" >&2
  exit 1
fi
# shellcheck disable=SC2016
if ! grep -Fq "release-passport-impact-json: \${{ contains(steps.target_ref.outputs.target_ref, '/v1.2') && '.buildchain/release-impact.json' || '' }}" "$promotion_workflow"; then
  echo "Buildchain promotion must supply the v1.2 production release passport impact ledger" >&2
  exit 1
fi
# shellcheck disable=SC2016
if ! grep -Fq "publish-required-artifacts-json: \${{ contains(steps.target_ref.outputs.target_ref, '/v1.2') && steps.required_artifacts.outputs.json || '' }}" "$promotion_workflow"; then
  echo "Buildchain promotion must require the exact five-image OCI family" >&2
  exit 1
fi
# shellcheck disable=SC2016
if ! grep -Fq 'python3 scripts/required-publish-artifacts.py --github-output "$GITHUB_OUTPUT"' "$promotion_workflow"; then
  echo "Buildchain promotion must resolve image requirements from repository manifests" >&2
  exit 1
fi
if ! grep -Fq 'fetch-depth: 0' "$promotion_workflow"; then
  echo "Buildchain promotion must fetch history for the trusted lock acceptance baseline" >&2
  exit 1
fi
