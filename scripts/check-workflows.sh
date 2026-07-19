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
if [ "$(grep -Fc 'release-passport-impact-json: ".buildchain/release-impact.json"' "$promotion_workflow")" -ne 2 ]; then
  echo "Buildchain promotion must supply the release passport impact ledger on both channels" >&2
  exit 1
fi
# shellcheck disable=SC2016
if [ "$(grep -Fc 'publish-required-artifacts-json: ${{ steps.required_artifacts.outputs.json }}' "$promotion_workflow")" -ne 2 ]; then
  echo "Buildchain promotion must require the exact manifest-declared OCI family" >&2
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

kungfu_package_workflow="$repo_root/.github/workflows/comparator-kungfu-package-smoke.yml"
# shellcheck disable=SC2016
if ! grep -Fq 'name: ${{ inputs.package_artifact_name }}' "$kungfu_package_workflow"; then
  echo "Kungfu qualification must download the caller-provided Actions artifact" >&2
  exit 1
fi
if ! grep -Fq 'KUNGFU_CLI_ARTIFACT_NAME: kungfu-episodes-cli-linux-x64.tar.gz' "$kungfu_package_workflow" ||
   grep -Fq 'KUNGFU_CLI_ARTIFACT_NAME: ${{ inputs.package_artifact_name }}' "$kungfu_package_workflow"; then
  echo "Kungfu smoke must receive the fixed package filename, not the Actions artifact name" >&2
  exit 1
fi

public_verifier="$repo_root/scripts/verify-ghcr-public.sh"
if grep -Fq 'api.github.com/orgs/' "$public_verifier" ||
   ! grep -Fq 'https://ghcr.io/token?scope=repository:' "$public_verifier"; then
  echo "GHCR public verification must use anonymous registry authority without package API scope" >&2
  exit 1
fi
