#!/bin/bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
image_tag=""
require_channel_match="false"
remote_name="${BUILDCHAIN_RELEASE_REMOTE:-origin}"

usage() {
  cat <<'EOF'
Usage: scripts/verify-buildchain-release-source.sh --tag <vX.Y.Z[-alpha.N]> [--require-channel-match]

Verifies that an image release tag matches Buildchain version state. With
--require-channel-match, also requires the matching Buildchain channel branch to
point at the same commit as the tag.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --tag)
      image_tag="${2:-}"
      shift 2
      ;;
    --require-channel-match)
      require_channel_match="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ -z "$image_tag" ]; then
  echo "--tag is required" >&2
  exit 2
fi

case "$image_tag" in
  v*.*.*|v*.*.*-alpha.*) ;;
  *)
    echo "Refusing non-Buildchain exact release tag: $image_tag" >&2
    exit 1
    ;;
esac

release_version="${image_tag#v}"

if ! [[ "$release_version" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)(-alpha\.[0-9]+)?$ ]]; then
  echo "Refusing unsupported Buildchain release version: $release_version" >&2
  exit 1
fi

major="${BASH_REMATCH[1]}"
minor="${BASH_REMATCH[2]}"
suffix="${BASH_REMATCH[4]}"
channel="release"
if [ -n "$suffix" ]; then
  channel="alpha"
fi
channel_branch="${channel}/v${major}/v${major}.${minor}"

package_version="$(
  python3 - "$repo_root/package.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle)["version"])
PY
)"

if [ "$package_version" != "$release_version" ]; then
  echo "Buildchain version-state mismatch: package.json has ${package_version}, expected ${release_version}" >&2
  exit 1
fi

if [ "$require_channel_match" = "true" ]; then
  tag_sha="$(git -C "$repo_root" rev-list -n 1 "$image_tag" 2>/dev/null || git -C "$repo_root" rev-parse HEAD)"
  if ! git -C "$repo_root" ls-remote --exit-code --heads "$remote_name" "$channel_branch" >/dev/null 2>&1; then
    echo "Buildchain channel branch is missing on ${remote_name}: ${channel_branch}" >&2
    echo "Create/promote through the Buildchain channel before publishing ${image_tag}." >&2
    exit 1
  fi
  git -C "$repo_root" fetch --no-tags "$remote_name" "refs/heads/${channel_branch}:refs/remotes/${remote_name}/${channel_branch}"
  channel_sha="$(git -C "$repo_root" rev-parse "refs/remotes/${remote_name}/${channel_branch}")"
  if [ "$tag_sha" != "$channel_sha" ]; then
    echo "Buildchain channel mismatch: ${image_tag} points at ${tag_sha}, but ${channel_branch} points at ${channel_sha}" >&2
    exit 1
  fi
fi

echo "Buildchain release source verified: tag=${image_tag} version=${release_version} channel=${channel_branch}"
