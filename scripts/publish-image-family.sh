#!/bin/bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

required_env() {
  local name="$1"
  local value="${!name:-}"
  if [ -z "$value" ]; then
    echo "${name} is required" >&2
    exit 2
  fi
}

required_env BUILDCHAIN_VERSION
required_env BUILDCHAIN_CHANNEL
required_env BUILDCHAIN_SOURCE_SHA
required_env BUILDCHAIN_RELEASE_SHA
required_env BUILDCHAIN_TARGET_REF
required_env BUILDCHAIN_PUBLISH_EVIDENCE

image_tag="v${BUILDCHAIN_VERSION}"
evidence_dir="${BUILDCHAIN_EVIDENCE_DIR:-$(dirname "$BUILDCHAIN_PUBLISH_EVIDENCE")}"
summary_path="${evidence_dir}/image-digests.json"

mkdir -p "$evidence_dir"

export BUILDCHAIN_REUSE_EXISTING_IMAGES="${BUILDCHAIN_REUSE_EXISTING_IMAGES:-true}"

bash "$repo_root/scripts/build-image-family.sh" \
  --tag "$image_tag" \
  --push \
  --summary "$summary_path"

bash "$repo_root/scripts/verify-ghcr-public.sh" --tag "$image_tag"

python3 "$repo_root/scripts/write-publish-evidence.py" \
  --summary "$summary_path" \
  --output "$BUILDCHAIN_PUBLISH_EVIDENCE"
