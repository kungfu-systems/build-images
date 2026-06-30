#!/bin/bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
registry="ghcr.io/kungfu-systems/build-images"
image_tag=""

usage() {
  cat <<'EOF'
Usage: scripts/verify-ghcr-public.sh --tag <tag> [--registry <registry>]

Verifies that all publishable image packages are public in GitHub Packages and
that the just-published tag can be resolved through the anonymous GHCR pull
path.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --tag)
      image_tag="${2:-}"
      shift 2
      ;;
    --registry)
      registry="${2:-}"
      shift 2
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

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required for GHCR visibility verification" >&2
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "jq is required for GHCR visibility verification" >&2
  exit 1
fi

registry_host="${registry%%/*}"
registry_path="${registry#*/}"
owner="${registry_path%%/*}"
repo="${registry_path#*/}"

if [ "$registry_host" != "ghcr.io" ] || [ "$owner" = "$registry_path" ] || [ -z "$repo" ]; then
  echo "Expected registry in the form ghcr.io/<owner>/<repo>, got: $registry" >&2
  exit 2
fi

api_token="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
auth_args=()
if [ -n "$api_token" ]; then
  auth_args=(-H "Authorization: Bearer $api_token")
fi

plan_json="$(mktemp)"
trap 'rm -f "$plan_json"' EXIT
python3 "$repo_root/scripts/resolve-image-dag.py" --json > "$plan_json"

image_count="$(python3 - "$plan_json" <<'PY'
import json, sys
images = json.load(open(sys.argv[1], encoding="utf-8"))["images"]
print(len([image for image in images if image.get("publish") is True]))
PY
)"

checked=0
while [ "$checked" -lt "$image_count" ]; do
  image_name="$(python3 - "$plan_json" "$checked" <<'PY'
import json, sys
images = json.load(open(sys.argv[1], encoding="utf-8"))["images"]
publishable = [image for image in images if image.get("publish") is True]
print(publishable[int(sys.argv[2])]["name"])
PY
)"
  package_name="${repo}/${image_name}"
  package_path="${package_name//\//%2F}"

  package_json="$(
    curl -fsS \
      "${auth_args[@]}" \
      -H "Accept: application/vnd.github+json" \
      -H "X-GitHub-Api-Version: 2022-11-28" \
      "https://api.github.com/orgs/${owner}/packages/container/${package_path}"
  )"
  visibility="$(printf '%s' "$package_json" | jq -r '.visibility // empty')"
  if [ "$visibility" != "public" ]; then
    echo "GHCR package is not public: ${package_name} visibility=${visibility:-unknown}" >&2
    echo "Open https://github.com/orgs/${owner}/packages/container/package/${package_path}/settings and change visibility to Public." >&2
    echo "Also check https://github.com/organizations/${owner}/settings/packages for the organization package creation/default policy." >&2
    exit 1
  fi

  pull_repo="${owner}/${repo}/${image_name}"
  pull_token="$(curl -fsS "https://ghcr.io/token?scope=repository:${pull_repo}:pull" | jq -r '.token // empty')"
  if [ -z "$pull_token" ]; then
    echo "GHCR did not issue an anonymous pull token for ${pull_repo}" >&2
    exit 1
  fi

  headers="$(
    curl -fsSI \
      -H "Authorization: Bearer $pull_token" \
      -H "Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json" \
      "https://ghcr.io/v2/${pull_repo}/manifests/${image_tag}"
  )"
  http_code="$(printf '%s\n' "$headers" | awk 'toupper($0) ~ /^HTTP\// {code=$2} END {print code}')"
  digest="$(printf '%s\n' "$headers" | awk 'BEGIN{IGNORECASE=1} /^docker-content-digest:/ {print $2}' | tr -d '\r')"
  if [ "$http_code" != "200" ] || [ -z "$digest" ]; then
    echo "Anonymous GHCR manifest check failed for ${pull_repo}:${image_tag} http=${http_code:-unknown}" >&2
    exit 1
  fi

  echo "${package_name}:${image_tag} public digest=${digest}"
  checked=$((checked + 1))
done
