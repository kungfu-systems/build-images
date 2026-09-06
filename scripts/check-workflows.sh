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
if ! grep -Fq 'release-candidate-promote.yml@v4-alpha' "$promotion_workflow" ||
   ! grep -Fq 'publish-artifact-kind: oci' "$promotion_workflow" ||
   ! grep -Fq 'declarative-release-tail: true' "$promotion_workflow" ||
   ! grep -Fq 'publication-auto-admission: true' "$promotion_workflow" ||
   ! grep -Fq 'publication-target: oci:ghcr.io/kungfu-systems/build-images' "$promotion_workflow" ||
   ! grep -Fq 'packages: write' "$promotion_workflow"; then
  echo "OCI publication must use the public v4 candidate provider with explicit registry authority" >&2
  exit 1
fi
if grep -Eq 'publish-command:|actions/promote-buildchain-ref@|lifecycle.publish' "$promotion_workflow" "$repo_root/.buildchain/buildchain.toml"; then
  echo "Consumer publication commands are not part of the v4 provider plane" >&2
  exit 1
fi
if ! grep -Fq 'build.yml@v4-alpha' "$repo_root/.github/workflows/build.yml" ||
   ! grep -Fq 'release-candidate: true' "$repo_root/.github/workflows/build.yml"; then
  echo "OCI images must be sealed by the public v4 build candidate workflow" >&2
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

renderer_workflow="$repo_root/.github/workflows/demo-renderer-qualification.yml"
for required in \
  "actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09" \
  "docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c" \
  "anchore/sbom-action@e22c389904149dbc22b58101806040fa8d37a610" \
  "aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25" \
  "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" \
  "docker run --rm --network none --read-only" \
  "compression-level: 0" \
  "overwrite: false"
do
  if ! grep -Fq "$required" "$renderer_workflow"; then
    echo "Demo renderer qualification is missing required evidence boundary: $required" >&2
    exit 1
  fi
done
