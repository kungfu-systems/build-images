#!/bin/bash
set -euo pipefail

expected_source="d6fb3879c8f495b6b4e4a1a619ce78358291a6e1"
image_ref=""
source_dir=""
output_dir=""

usage() {
  cat <<'EOF'
Usage: scripts/smoke-kungfu-native-source-build.sh \
  --image <ghcr.io/.../kungfu-native-linux-x64@sha256:...> \
  --source <fresh-kungfu-worktree> \
  --output <evidence-directory>

Builds the exact Kungfu source fixture inside the exact image, then verifies
the ELF and dynamic links on the host. It does not run performance workloads.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --image) image_ref="${2:-}"; shift 2 ;;
    --source) source_dir="${2:-}"; shift 2 ;;
    --output) output_dir="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ ! "$image_ref" =~ ^ghcr\.io/kungfu-systems/build-images/kungfu-native-linux-x64@sha256:[0-9a-f]{64}$ ]]; then
  echo "--image must be the published kungfu-native-linux-x64 image by exact digest" >&2
  exit 2
fi
for value in "$source_dir" "$output_dir"; do
  if [ -z "$value" ]; then
    echo "--source and --output are required" >&2
    exit 2
  fi
done
for name in COREPACK_NPM_REGISTRY NPM_CONFIG_REGISTRY NODEJS_ORG_MIRROR \
  UV_PYTHON_INSTALL_MIRROR KUNGFU_CONAN_REMOTE_URL KF_LIBWASM_CARGO_REGISTRY; do
  value="${!name:-}"
  if [[ "$value" =~ ://[^/]*@ ]]; then
    echo "$name must not contain embedded credentials" >&2
    exit 2
  fi
  if [[ "$value" == *\?* || "$value" == *\#* ]]; then
    echo "$name must not contain query parameters or fragments" >&2
    exit 2
  fi
done
if [[ "${COREPACK_NPM_REGISTRY:-https://registry.npmjs.org}" == */ ]]; then
  echo "COREPACK_NPM_REGISTRY must not end in /" >&2
  exit 2
fi

source_dir="$(cd "$source_dir" && pwd)"
mkdir -p "$output_dir"
output_dir="$(cd "$output_dir" && pwd)"
if [ -n "$(find "$output_dir" -mindepth 1 -print -quit)" ]; then
  echo "--output must be an empty evidence directory" >&2
  exit 2
fi
test "$(git -C "$source_dir" rev-parse HEAD)" = "$expected_source"
test -z "$(git -C "$source_dir" status --porcelain)"
test -z "$(git -C "$source_dir" clean -ndx)"
git_common_dir="$(git -C "$source_dir" rev-parse --path-format=absolute --git-common-dir)"
case "$git_common_dir" in
  "$source_dir"|"$source_dir"/*)
    git_mount_args=()
    ;;
  *)
    git_mount_args=(-v "$git_common_dir:$git_common_dir:ro")
    ;;
esac

home_dir="$(mktemp -d "${TMPDIR:-/tmp}/kungfu-native-home.XXXXXX")"
cleanup() {
  rm -rf "$home_dir"
}
trap cleanup EXIT

docker pull "$image_ref"
resolved_digests="$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$image_ref")"
if ! grep -Fxq "$image_ref" <<<"$resolved_digests"; then
  echo "Pulled image digest mismatch: $resolved_digests" >&2
  exit 1
fi

docker run --rm \
  --platform linux/amd64 \
  --user "$(id -u):$(id -g)" \
  -e HOME=/home/kungfu \
  -e USER=kungfu \
  -e KUNGFU_SOURCE_SHA="$expected_source" \
  -e KUNGFU_BUILD_EVIDENCE_DIR=/evidence \
  -e KUNGFU_BUILD_JOBS="${KUNGFU_BUILD_JOBS:-12}" \
  -e COREPACK_NPM_REGISTRY="${COREPACK_NPM_REGISTRY:-https://registry.npmjs.org}" \
  -e NPM_CONFIG_REGISTRY="${NPM_CONFIG_REGISTRY:-https://registry.npmjs.org}" \
  -e NODEJS_ORG_MIRROR="${NODEJS_ORG_MIRROR:-}" \
  -e UV_PYTHON_INSTALL_MIRROR="${UV_PYTHON_INSTALL_MIRROR:-}" \
  -e KUNGFU_CONAN_REMOTE_URL="${KUNGFU_CONAN_REMOTE_URL:-}" \
  -e KF_LIBWASM_CARGO_REGISTRY="${KF_LIBWASM_CARGO_REGISTRY:-sparse+https://index.crates.io/}" \
  -e GIT_OPTIONAL_LOCKS=0 \
  -v "$source_dir:/work" \
  -v "$home_dir:/home/kungfu" \
  -v "$output_dir:/evidence" \
  "${git_mount_args[@]}" \
  "$image_ref" \
  /opt/kungfu-native-source-build/bin/build-kungfu-core

fixture="$source_dir/framework/core/build/Release/kungfu_durability_slo_fixture"
file_output="$(file "$fixture")"
case "$file_output" in
  *ELF\ 64-bit*X86-64*|*ELF\ 64-bit*x86-64*) ;;
  *) echo "Unexpected fixture format: $file_output" >&2; exit 1 ;;
esac
ldd_output="$(ldd "$fixture")"
if grep -Fq "not found" <<<"$ldd_output"; then
  echo "$ldd_output" >&2
  exit 1
fi
test "$(git -C "$source_dir" rev-parse HEAD)" = "$expected_source"
test -z "$(git -C "$source_dir" status --porcelain)"

python3 - "$output_dir/consumer-build.json" "$output_dir/consumer-smoke.json" \
  "$image_ref" "$file_output" "$ldd_output" "$(hostname)" "$(id -u):$(id -g)" <<'PY'
import json
import platform
import sys
from pathlib import Path

build_path, output_path, image, file_output, ldd_output, hostname, uid_gid = sys.argv[1:]
build = json.loads(Path(build_path).read_text())
payload = {
    "schema": "kungfu-native-source-build-smoke/v1",
    "image": image,
    "source_sha": build["source_sha"],
    "build_jobs": build["build_jobs"],
    "libnode_package": build["libnode_package"],
    "host": {
        "hostname": hostname,
        "machine": platform.machine(),
        "uid_gid": uid_gid,
    },
    "fixture": {
        "path": build["fixture_path"],
        "sha256": build["fixture_sha256"],
        "file": file_output,
        "ldd": ldd_output.splitlines(),
    },
    "build_log_sha256": build["build_log_sha256"],
    "build_identity_sha256": build["build_identity_sha256"],
    "tool_versions": build["tool_versions"],
    "cache_inputs": build["cache_inputs"],
    "shifu_path": build["shifu_path"],
    "source_clean": True,
    "performance_run": False,
    "authority": "build-reproducibility-only",
    "next_step": "Run performance workloads directly on the named host filesystem.",
}
Path(output_path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
PY

cat "$output_dir/consumer-smoke.json"
echo "Fresh consumer smoke passed; host-native performance remains a separate step."
