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
log_path="${BUILDCHAIN_LOG_PATH:-${evidence_dir}/buildchain-events.jsonl}"
log_summary_path="${evidence_dir}/buildchain-log-summary.json"

mkdir -p "$evidence_dir"

export BUILDCHAIN_REUSE_EXISTING_IMAGES="${BUILDCHAIN_REUSE_EXISTING_IMAGES:-true}"
export BUILDCHAIN_LOG_PATH="$log_path"

bash "$repo_root/scripts/buildchain-toolkit.sh" span \
  --event image.family \
  --phase build \
  --component build-image-family \
  --attribute tag="$image_tag" \
  --attribute publish=true \
  -- \
  bash "$repo_root/scripts/build-image-family.sh" \
  --tag "$image_tag" \
  --push \
  --summary "$summary_path"

bash "$repo_root/scripts/verify-ghcr-public.sh" --tag "$image_tag"

bash "$repo_root/scripts/buildchain-toolkit.sh" verify observability-log "$log_path" \
  --min-events 2 \
  --require-phase build \
  --require-component build-image-family \
  --require-event image.family.start \
  --require-event image.family.end

bash "$repo_root/scripts/buildchain-toolkit.sh" log summary \
  --path "$log_path" \
  --json > "$log_summary_path"

python3 "$repo_root/scripts/write-publish-evidence.py" \
  --summary "$summary_path" \
  --output "$BUILDCHAIN_PUBLISH_EVIDENCE"
