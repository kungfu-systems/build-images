#!/bin/bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
registry="ghcr.io/kungfu-systems/build-images"
image_tag=""
push_images="false"
summary_path="$repo_root/build/image-digests.json"
plan_path=""
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
Usage: scripts/build-image-family.sh --tag <tag> [--push] [--registry <registry>] [--summary <path>] [--plan <path>]

Builds or reuses the manifest-defined image family, verifies every immutable
digest, runs every image smoke policy, and optionally pushes exact tags.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --tag) image_tag="${2:-}"; shift 2 ;;
    --push) push_images="true"; shift ;;
    --registry) registry="${2:-}"; shift 2 ;;
    --summary) summary_path="${2:-}"; shift 2 ;;
    --plan) plan_path="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
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
summary_lines="$tmp_dir/summary.jsonl"
: > "$summary_lines"

if [ -z "$plan_path" ]; then
  plan_path="$tmp_dir/image-plan.json"
  python3 "$repo_root/scripts/resolve-image-dag.py" --json | python3 -c '
import json, sys
payload = json.load(sys.stdin)
platforms = {"linux-x64": "linux/amd64", "linux-arm64": "linux/arm64"}
for image in payload["images"]:
    image["platform"] = platforms[image["platform"]]
    image["action"] = "built"
payload["selection"] = {
    "mode": "full", "full_rebuild": True,
    "selected_images": [image["name"] for image in payload["images"]],
    "changed_paths": [], "invalidators": [{"path": "", "reason": "explicit-full-build"}],
    "reasons": {},
}
json.dump(payload, sys.stdout, indent=2)
sys.stdout.write("\n")
' > "$plan_path"
fi

if [ "$push_images" != "true" ] && python3 - "$plan_path" <<'PY'
import json, sys
raise SystemExit(0 if any(image.get("action") == "reused" for image in json.load(open(sys.argv[1]))["images"]) else 1)
PY
then
  echo "cross-version reuse requires --push so the exact target tag can be verified" >&2
  exit 1
fi

json_field() {
  python3 - "$1" "$2" "$3" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))["images"][int(sys.argv[2])]
value = data
for key in sys.argv[3].split("."):
    value = value.get(key) if isinstance(value, dict) else None
if value is None:
    print("")
elif isinstance(value, (dict, list)):
    print(json.dumps(value, separators=(",", ":"), sort_keys=True))
else:
    print(value)
PY
}

summary_digest() {
  python3 - "$summary_lines" "$1" <<'PY'
import json, sys
for line in open(sys.argv[1], encoding="utf-8"):
    record = json.loads(line)
    if record["name"] == sys.argv[2]:
        print(record["digest"])
        raise SystemExit(0)
raise SystemExit(1)
PY
}

manifest_exists() {
  python3 - "$1" <<'PY'
import json, sys
raise SystemExit(0 if json.load(open(sys.argv[1], encoding="utf-8"))["exists"] else 1)
PY
}

manifest_digest() {
  python3 - "$1" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("digest", ""))
PY
}

image_count="$(python3 - "$plan_path" <<'PY'
import json, sys
print(len(json.load(open(sys.argv[1], encoding="utf-8"))["images"]))
PY
)"

i=0
while [ "$i" -lt "$image_count" ]; do
  image_name="$(json_field "$plan_path" "$i" name)"
  image_path="$(json_field "$plan_path" "$i" path)"
  base_image="$(json_field "$plan_path" "$i" base)"
  action="$(json_field "$plan_path" "$i" action)"
  platform="$(json_field "$plan_path" "$i" platform)"
  contract_major="$(json_field "$plan_path" "$i" contract_major)"
  lock_status="$(python3 - "$repo_root/$image_path/image.toml" <<'PY'
import sys, tomllib
print(tomllib.load(open(sys.argv[1], "rb")).get("lock", {}).get("status", ""))
PY
)"
  image_repository="${registry}/${image_name}"
  image_ref="${image_repository}:${image_tag}"
  parent_digest=""
  if [ -n "$base_image" ]; then
    parent_digest="$(summary_digest "$base_image")"
  fi

  content_version="$buildchain_version"
  content_ref="$image_tag"
  content_source_sha="$buildchain_source_sha"
  content_material_sha="$buildchain_release_material_sha"
  digest=""
  target_manifest="$tmp_dir/${image_name}-target.json"
  evidence_rel="image-evidence/${image_name}"
  evidence_dir="$(dirname "$summary_path")/${evidence_rel}"
  registry_evidence="$evidence_dir/public-manifest.json"
  inspect_evidence="$evidence_dir/image-inspect.json"
  smoke_evidence="$evidence_dir/smoke.json"
  smoke_log="$evidence_dir/smoke.log"
  mkdir -p "$evidence_dir"

  if [ "$push_images" = "true" ]; then
    target_manifest_args=(
      --repository "$image_repository"
      --ref "$image_tag"
      --allow-missing
      --output "$target_manifest"
    )
    if [ "$lock_status" = "pending-first-publish" ]; then
      target_manifest_args+=(--allow-missing-package)
    fi
    python3 "$repo_root/scripts/ghcr-manifest.py" \
      "${target_manifest_args[@]}" >/dev/null
  fi

  if [ "$action" = "reused" ]; then
    baseline_repository="$(json_field "$plan_path" "$i" baseline.image)"
    baseline_digest="$(json_field "$plan_path" "$i" baseline.digest)"
    baseline_ref="$(json_field "$plan_path" "$i" baseline.release.ref)"
    baseline_parent="$(json_field "$plan_path" "$i" baseline.parent_digest)"
    content_version="$(json_field "$plan_path" "$i" baseline.content.version)"
    content_ref="$(json_field "$plan_path" "$i" baseline.content.ref)"
    content_source_sha="$(json_field "$plan_path" "$i" baseline.content.source_sha)"
    content_material_sha="$(json_field "$plan_path" "$i" baseline.content.material_sha)"
    if [ "$baseline_repository" != "$image_repository" ]; then
      echo "Baseline repository mismatch for ${image_name}" >&2
      exit 1
    fi
    if [ "$baseline_parent" != "$parent_digest" ]; then
      echo "Baseline parent digest mismatch for ${image_name}" >&2
      exit 1
    fi

    echo "::group::verify reuse source ${baseline_repository}:${baseline_ref}"
    python3 "$repo_root/scripts/ghcr-manifest.py" \
      --repository "$baseline_repository" \
      --ref "$baseline_ref" \
      --expected-digest "$baseline_digest" \
      --attempts 3 >/dev/null
    python3 "$repo_root/scripts/ghcr-manifest.py" \
      --repository "$baseline_repository" \
      --ref "$baseline_digest" \
      --expected-digest "$baseline_digest" \
      --attempts 3 >/dev/null
    echo "::endgroup::"

    digest="$baseline_digest"
    if manifest_exists "$target_manifest"; then
      observed="$(manifest_digest "$target_manifest")"
      if [ "$observed" != "$digest" ]; then
        echo "Existing exact tag ${image_ref} has ${observed}, expected reused ${digest}" >&2
        exit 1
      fi
    else
      echo "::group::alias ${image_ref} -> ${image_repository}@${digest}"
      docker buildx imagetools create --prefer-index=false --tag "$image_ref" "${image_repository}@${digest}"
      echo "::endgroup::"
    fi
  elif [ "$action" = "built" ]; then
    if [ "$push_images" = "true" ] && manifest_exists "$target_manifest"; then
      if [ "$reuse_existing_images" != "true" ]; then
        echo "Exact tag already exists and rerun reuse is disabled: ${image_ref}" >&2
        exit 1
      fi
      digest="$(manifest_digest "$target_manifest")"
      echo "Reusing current-release artifact ${image_ref}@${digest}"
    else
      build_args=(
        --platform "$platform"
        --label "org.opencontainers.image.source=https://github.com/kungfu-systems/build-images"
        --label "org.opencontainers.image.version=${buildchain_version}"
        --label "org.opencontainers.image.revision=${buildchain_source_sha}"
        --label "io.kungfu.image.contract-major=${contract_major}"
        --label "io.kungfu.image.platform=${platform}"
      )
      if [ -n "$base_image" ]; then
        build_args+=(
          --build-arg "BASE_IMAGE=${registry}/${base_image}@${parent_digest}"
          --label "io.kungfu.image.parent-digest=${parent_digest}"
        )
      fi
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

      echo "::group::build ${image_ref}"
      docker build "${build_args[@]}" -t "$image_ref" "$repo_root/$image_path"
      echo "::endgroup::"
      if [ "$push_images" = "true" ]; then
        docker push "$image_ref"
      else
        digest="$(docker image inspect --format '{{.Id}}' "$image_ref")"
      fi
    fi
  else
    echo "Unsupported publish action for ${image_name}: ${action}" >&2
    exit 1
  fi

  if [ "$push_images" = "true" ]; then
    manifest_args=(
      --repository "$image_repository"
      --ref "$image_tag"
      --attempts 6
      --output "$registry_evidence"
    )
    if [ -n "$digest" ]; then
      manifest_args+=(--expected-digest "$digest")
    fi
    python3 "$repo_root/scripts/ghcr-manifest.py" "${manifest_args[@]}" >/dev/null
    digest="$(manifest_digest "$registry_evidence")"
    immutable_ref="${image_repository}@${digest}"
    echo "::group::pull ${immutable_ref}"
    docker pull "$immutable_ref"
    echo "::endgroup::"

    inspect_args=(
      --image "$immutable_ref"
      --platform "$platform"
      --contract-major "$contract_major"
      --content-version "$content_version"
      --content-source-sha "$content_source_sha"
      --content-material-sha "$content_material_sha"
      --output "$inspect_evidence"
    )
    if [ -n "$parent_digest" ]; then
      inspect_args+=(--parent-digest "$parent_digest")
    fi
    python3 "$repo_root/scripts/verify-published-image.py" "${inspect_args[@]}" >/dev/null
    smoke_ref="$immutable_ref"
  else
    smoke_ref="$image_ref"
  fi

  : > "$smoke_log"
  test_count="$(python3 - "$repo_root/$image_path/image.toml" <<'PY'
import sys, tomllib
print(len(tomllib.load(open(sys.argv[1], "rb"))["build"]["test_commands"]))
PY
)"
  j=0
  while [ "$j" -lt "$test_count" ]; do
    test_command="$(python3 - "$repo_root/$image_path/image.toml" "$j" <<'PY'
import sys, tomllib
print(tomllib.load(open(sys.argv[1], "rb"))["build"]["test_commands"][int(sys.argv[2])])
PY
)"
    echo "::group::smoke ${image_name}: ${test_command}"
    docker run --rm "$smoke_ref" sh -lc "$test_command" 2>&1 | tee -a "$smoke_log"
    echo "::endgroup::"
    j=$((j + 1))
  done

  python3 - "$smoke_log" "$smoke_evidence" "$image_name" "$action" <<'PY'
import hashlib, json, sys
from pathlib import Path
log_path, output_path, name, action = sys.argv[1:]
data = Path(log_path).read_bytes()
payload = {
    "schema": 1,
    "policy": "image-manifest-test-commands-v1",
    "passed": True,
    "image": name,
    "action": action,
    "log_sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
}
Path(output_path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

  python3 - "$summary_lines" "$plan_path" "$i" "$image_repository" "$image_ref" "$image_tag" "$digest" "$parent_digest" "$content_version" "$content_ref" "$content_source_sha" "$content_material_sha" "$buildchain_version" "$buildchain_target_ref" "$buildchain_source_sha" "$buildchain_release_material_sha" "$evidence_rel/public-manifest.json" "$evidence_rel/smoke.json" "$push_images" <<'PY'
import json, sys
(
    output, plan_path, index, repository, image_ref, tag, digest, parent_digest,
    content_version, content_ref, content_source, content_material,
    release_version, target_ref, release_source, release_material,
    registry_evidence, smoke_evidence, pushed,
) = sys.argv[1:]
image = json.load(open(plan_path, encoding="utf-8"))["images"][int(index)]
pushed = pushed == "true"
record = {
    "name": image["name"],
    "repository": repository,
    "image": image_ref,
    "tag": tag,
    "base": image.get("base"),
    "digest": digest,
    "action": image["action"],
    "platform": image["platform"],
    "contract_major": image["contract_major"],
    "parent_digest": parent_digest or None,
    "content": {
        "version": content_version,
        "ref": content_ref,
        "source_sha": content_source,
        "material_sha": content_material,
    },
    "release": {
        "version": release_version,
        "ref": tag,
        "target_ref": target_ref,
        "source_sha": release_source,
        "material_sha": release_material,
    },
    "verification": {
        "public_manifest": pushed,
        "ref": tag,
        "digest": digest,
        "platform": image["platform"],
        "contract_major": image["contract_major"],
        "evidence": registry_evidence,
        "smoke": {
            "policy": "image-manifest-test-commands-v1",
            "passed": True,
            "evidence": smoke_evidence,
        },
    },
    "test_commands": image.get("test_commands", []),
}
if parent_digest:
    record["verification"]["parent_digest"] = parent_digest
with open(output, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(record, sort_keys=True) + "\n")
PY

  i=$((i + 1))
done

mkdir -p "$(dirname "$summary_path")"
python3 - "$summary_lines" "$summary_path" "$push_images" "$plan_path" <<'PY'
import json, sys
lines, output, pushed, plan_path = sys.argv[1:]
records = [json.loads(line) for line in open(lines, encoding="utf-8") if line.strip()]
plan = json.load(open(plan_path, encoding="utf-8"))
payload = {
    "schema": 1,
    "pushed": pushed == "true",
    "selection": plan.get("selection", {}),
    "baseline": plan.get("baseline", {}),
    "images": records,
}
with open(output, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2)
    handle.write("\n")
print(json.dumps(payload, indent=2))
PY
