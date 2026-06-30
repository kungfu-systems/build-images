#!/bin/bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
registry="ghcr.io/kungfu-systems/build-images"
image_tag=""
push_images="false"
summary_path="$repo_root/build/image-digests.json"

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
  build_args=()
  if [ -n "$base_image" ]; then
    build_args+=(--build-arg "BASE_IMAGE=${registry}/${base_image}:${image_tag}")
  fi

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
  else
    digest="$(docker image inspect --format '{{.Id}}' "$image_ref")"
  fi

  source_sha="${GITHUB_SHA:-local}"
  python3 - "$summary_lines" "$image_name" "$image_ref" "$image_tag" "$base_image" "$digest" "$source_sha" <<'PY'
import json, sys
path, name, ref, tag, base, digest, source = sys.argv[1:]
record = {
    "name": name,
    "image": ref,
    "tag": tag,
    "base": base or None,
    "digest": digest,
    "source": source,
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
