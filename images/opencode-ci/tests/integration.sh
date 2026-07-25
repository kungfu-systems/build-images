#!/bin/bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
run_id="opencode-ci-$PPID-$$"
network="${run_id}-network"
mock="${run_id}-mock"
base_image="${run_id}-base"
verify_image="${run_id}-verify"
image="${run_id}-image"
fixture_root="$(mktemp -d)"

cleanup() {
  docker rm -f "$mock" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker build --platform linux/amd64 -t "$base_image" "$repo_root/images/base-linux"
verify_build_args=(--platform linux/amd64 --build-arg "BASE_IMAGE=$base_image")
if [ -n "${APT_HTTP_PROXY:-}" ]; then
  verify_build_args+=(--build-arg "APT_HTTP_PROXY=$APT_HTTP_PROXY")
fi
if [ -n "${PIP_INDEX_URL:-}" ]; then
  verify_build_args+=(--build-arg "PIP_INDEX_URL=$PIP_INDEX_URL")
fi
if [ -n "${PIP_TRUSTED_HOST:-}" ]; then
  verify_build_args+=(--build-arg "PIP_TRUSTED_HOST=$PIP_TRUSTED_HOST")
fi
docker build "${verify_build_args[@]}" -t "$verify_image" "$repo_root/images/kungfu-verify"
docker build --platform linux/amd64 --build-arg "BASE_IMAGE=$verify_image" -t "$image" "$repo_root/images/opencode-ci"
docker network create "$network" >/dev/null

run_case() {
  local mode="$1"
  local case_dir="$fixture_root/$mode"
  mkdir -p "$case_dir"
  printf '%s\n' "fixture input" >"$case_dir/input.txt"

  docker rm -f "$mock" >/dev/null 2>&1 || true
  docker run -d --rm --name "$mock" --network "$network" --network-alias mock \
    -e "MOCK_MODE=$mode" \
    -v "$repo_root/images/opencode-ci/tests/mock-openai.py:/mock-openai.py:ro" \
    python:3.12-slim python3 /mock-openai.py >/dev/null

  local ready=0
  for _attempt in 1 2 3 4 5 6 7 8 9 10; do
    if docker run --rm --network "$network" curlimages/curl:8.14.1 \
      -fsS http://mock:8080/health >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 1
  done
  [ "$ready" -eq 1 ] || { echo "mock endpoint did not become ready" >&2; return 1; }

  docker run --rm --platform linux/amd64 --network "$network" -v "$case_dir:/workspace" -w /workspace \
    -e OPENCODE_BASE_URL=http://mock:8080/v1 \
    -e OPENCODE_MODEL=fixture-model \
    -e OPENCODE_CONTEXT=65536 \
    -e OPENCODE_OUTPUT_DIR=/workspace/evidence \
    -e OPENCODE_PROMPT='Read /workspace/input.txt, write the exact requested output, and run the requested shell verification.' \
    -e OPENCODE_VERIFY_COMMAND='opencode-ci-verify /workspace/evidence/events.jsonl /workspace/output.txt /opt/opencode-ci/expected.txt /workspace/bash-ok.txt' \
    "$image" opencode-ci-run
}

run_case positive
if run_case false-success; then
  echo "negative fixture unexpectedly passed" >&2
  exit 1
fi

python3 - "$fixture_root/false-success/evidence/run.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["opencode_exit"] == 0, data
assert data["verifier_exit"] != 0, data
assert data["passed"] is False, data
PY
echo "OpenCode CI positive and false-success fixtures passed"
