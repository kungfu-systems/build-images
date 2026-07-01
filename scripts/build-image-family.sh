#!/bin/bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
registry="ghcr.io/kungfu-systems/build-images"
image_tag=""
push_images="false"
summary_path="$repo_root/build/image-digests.json"
reuse_existing_images="${BUILDCHAIN_REUSE_EXISTING_IMAGES:-false}"
buildchain_version="${BUILDCHAIN_VERSION:-}"
buildchain_channel="${BUILDCHAIN_CHANNEL:-}"
buildchain_source_sha="${BUILDCHAIN_SOURCE_SHA:-${GITHUB_SHA:-local}}"
buildchain_release_sha="${BUILDCHAIN_RELEASE_SHA:-}"
buildchain_release_material_sha="${BUILDCHAIN_RELEASE_MATERIAL_SHA:-}"
buildchain_publish_tooling_sha="${BUILDCHAIN_PUBLISH_TOOLING_SHA:-}"
buildchain_target_ref="${BUILDCHAIN_TARGET_REF:-}"

usage() {
  cat <<'EOF'
Usage: scripts/build-image-family.sh --tag <tag> [--push] [--registry <registry>] [--summary <path>]

Builds the manifest-defined image family in DAG order, runs each image's smoke
commands, and optionally pushes images to the registry.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --tag)
      image_tag="${2:-}"
      shift 2
      ;;
    --push)
      push_images="true"
      shift
      ;;
    --registry)
      registry="${2:-}"
      shift 2
      ;;
    --summary)
      summary_path="${2:-}"
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

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required for image builds" >&2
  exit 1
fi

if [ -z "$buildchain_version" ]; then
  buildchain_version="${image_tag#v}"
fi

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

plan_json="$tmp_dir/image-plan.json"
summary_lines="$tmp_dir/summary.jsonl"
python3 "$repo_root/scripts/resolve-image-dag.py" --json > "$plan_json"
: > "$summary_lines"

image_count="$(python3 - "$plan_json" <<'PY'
import json, sys
print(len(json.load(open(sys.argv[1], encoding="utf-8"))["images"]))
PY
)"

i=0
while [ "$i" -lt "$image_count" ]; do
  image_name="$(python3 - "$plan_json" "$i" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["images"][int(sys.argv[2])]["name"])
PY
)"
  image_path="$(python3 - "$plan_json" "$i" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["images"][int(sys.argv[2])]["path"])
PY
)"
  base_image="$(python3 - "$plan_json" "$i" <<'PY'
import json, sys
base = json.load(open(sys.argv[1], encoding="utf-8"))["images"][int(sys.argv[2])].get("base")
print(base or "")
PY
)"

  image_ref="${registry}/${image_name}:${image_tag}"
  image_repository="${registry}/${image_name}"
  build_args=()
  if [ -n "$base_image" ]; then
    build_args+=(--build-arg "BASE_IMAGE=${registry}/${base_image}:${image_tag}")
  fi
  build_args+=(
    --label "org.opencontainers.image.source=https://github.com/kungfu-systems/build-images"
    --label "org.opencontainers.image.version=${buildchain_version}"
    --label "org.opencontainers.image.revision=${buildchain_source_sha}"
  )
  if [ -n "$buildchain_channel" ]; then
    build_args+=(--label "io.kungfu.buildchain.channel=${buildchain_channel}")
  fi
  if [ -n "$buildchain_release_sha" ]; then
    build_args+=(--label "io.kungfu.buildchain.release-sha=${buildchain_release_sha}")
  fi
  if [ -n "$buildchain_release_material_sha" ]; then
    build_args+=(--label "io.kungfu.buildchain.release-material-sha=${buildchain_release_material_sha}")
  fi
  if [ -n "$buildchain_publish_tooling_sha" ]; then
    build_args+=(--label "io.kungfu.buildchain.publish-tooling-sha=${buildchain_publish_tooling_sha}")
  fi
  if [ -n "$buildchain_target_ref" ]; then
    build_args+=(--label "io.kungfu.buildchain.target-ref=${buildchain_target_ref}")
  fi

  reused_existing="false"
  digest=""
  if [ "$push_images" = "true" ] && [ "$reuse_existing_images" = "true" ]; then
    remote_digest="$(docker buildx imagetools inspect "$image_ref" --format '{{.Manifest.Digest}}' 2>/dev/null || true)"
    if [ -n "$remote_digest" ]; then
      echo "::group::reuse ${image_ref}"
      docker pull "$image_ref"
      remote_version="$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.version" }}' "$image_ref" 2>/dev/null || true)"
      remote_material_sha="$(docker image inspect --format '{{ index .Config.Labels "io.kungfu.buildchain.release-material-sha" }}' "$image_ref" 2>/dev/null || true)"
      if [ "$remote_version" != "$buildchain_version" ]; then
        echo "Existing image tag has version label ${remote_version:-<empty>}, expected ${buildchain_version}" >&2
        exit 1
      fi
      if [ -n "$buildchain_release_material_sha" ] && [ "$remote_material_sha" != "$buildchain_release_material_sha" ]; then
        echo "Existing image tag has material label ${remote_material_sha:-<empty>}, expected ${buildchain_release_material_sha}" >&2
        exit 1
      fi
      digest="$remote_digest"
      reused_existing="true"
      echo "Reusing existing ${image_ref}@${digest}"
      echo "::endgroup::"
    fi
  fi

  if [ "$reused_existing" != "true" ]; then
    echo "::group::build ${image_ref}"
    docker build "${build_args[@]}" -t "$image_ref" "$repo_root/$image_path"
    echo "::endgroup::"

    test_count="$(python3 - "$repo_root/$image_path/image.toml" <<'PY'
import sys, tomllib
data = tomllib.load(open(sys.argv[1], "rb"))
print(len(data["build"]["test_commands"]))
PY
)"
    j=0
    while [ "$j" -lt "$test_count" ]; do
      test_command="$(python3 - "$repo_root/$image_path/image.toml" "$j" <<'PY'
import sys, tomllib
data = tomllib.load(open(sys.argv[1], "rb"))
print(data["build"]["test_commands"][int(sys.argv[2])])
PY
)"
      echo "::group::test ${image_name}: ${test_command}"
      docker run --rm "$image_ref" sh -lc "$test_command"
      echo "::endgroup::"
      j=$((j + 1))
    done

    if [ "$push_images" = "true" ]; then
      docker push "$image_ref"
      digest="$(docker buildx imagetools inspect "$image_ref" --format '{{.Manifest.Digest}}')"
      echo "::group::pull ${image_name}@${digest}"
      docker pull "${registry}/${image_name}@${digest}"
      echo "::endgroup::"
    else
      digest="$(docker image inspect --format '{{.Id}}' "$image_ref")"
    fi
  fi

  python3 - "$summary_lines" "$image_name" "$image_repository" "$image_ref" "$image_tag" "$base_image" "$digest" "$buildchain_source_sha" "$buildchain_release_sha" "$buildchain_release_material_sha" "$buildchain_publish_tooling_sha" "$reused_existing" <<'PY'
import json, sys
(
    path,
    name,
    repository,
    ref,
    tag,
    base,
    digest,
    source,
    release_sha,
    release_material_sha,
    publish_tooling_sha,
    reused,
) = sys.argv[1:]
record = {
    "name": name,
    "repository": repository,
    "image": ref,
    "tag": tag,
    "base": base or None,
    "digest": digest,
    "source": source,
    "release_sha": release_sha or None,
    "release_material_sha": release_material_sha or None,
    "publish_tooling_sha": publish_tooling_sha or None,
    "reused": reused == "true",
}
with open(path, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(record, sort_keys=True) + "\n")
PY

  i=$((i + 1))
done

mkdir -p "$(dirname "$summary_path")"
python3 - "$summary_lines" "$summary_path" "$push_images" <<'PY'
import json, sys
lines, output, pushed = sys.argv[1:]
records = [json.loads(line) for line in open(lines, encoding="utf-8") if line.strip()]
payload = {"schema": 1, "pushed": pushed == "true", "images": records}
with open(output, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2)
    handle.write("\n")
print(json.dumps(payload, indent=2))
PY
