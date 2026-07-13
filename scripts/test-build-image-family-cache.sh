#!/bin/bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

fake_bin="$tmp_dir/bin"
mkdir -p "$fake_bin"

cat > "$fake_bin/docker" <<'EOF'
#!/bin/bash
set -euo pipefail

{
  printf '%s' "${1:-}"
  shift || true
  for argument in "$@"; do
    printf '\t%s' "$argument"
  done
  printf '\n'
} >> "$FAKE_DOCKER_LOG"

case "${1:-}" in
  version)
    exit 0
    ;;
  imagetools)
    printf '%s\n' 'sha256:remote'
    exit 0
    ;;
  build)
    exit 0
    ;;
esac

if [ "${1:-}" = "inspect" ]; then
  printf '%s\n' 'sha256:local'
fi
EOF
chmod +x "$fake_bin/docker"

run_family() {
  local mode="$1"
  local push="$2"
  local write="$3"
  local log_path="$4"
  local summary_path="$5"
  local args=(--tag v9.9.9 --registry registry.example/build-images --summary "$summary_path")
  if [ "$push" = "true" ]; then
    args+=(--push)
  fi

  : > "$log_path"
  PATH="$fake_bin:$PATH" \
    FAKE_DOCKER_LOG="$log_path" \
    BUILDCHAIN_REGISTRY_CACHE_MODE="$mode" \
    BUILDCHAIN_REGISTRY_CACHE_WRITE="$write" \
    BUILDCHAIN_REUSE_EXISTING_IMAGES=false \
    bash "$repo_root/scripts/build-image-family.sh" "${args[@]}" >/dev/null
}

read_log="$tmp_dir/read.log"
run_family auto false true "$read_log" "$tmp_dir/read-summary.json"
test "$(grep -c $'^buildx\tbuild' "$read_log")" -eq 5
test "$(grep -c -- '--cache-from' "$read_log")" -eq 5
test "$(grep -c -- '--cache-to' "$read_log" || true)" -eq 0
cache_ref_count="$(grep -o 'type=registry,ref=registry.example/build-images/[^[:space:]]*' "$read_log" | sort -u | wc -l | tr -d ' ')"
test "$cache_ref_count" -eq 5
grep -Fq -- $'--cache-from\ttype=registry,ref=registry.example/build-images/base-linux:buildcache-c1-linux-amd64-v1' "$read_log"
grep -Fq -- $'--build-arg\tBASE_IMAGE=registry.example/build-images/base-linux:v9.9.9' "$read_log"

write_log="$tmp_dir/write.log"
run_family auto true true "$write_log" "$tmp_dir/write-summary.json"
test "$(grep -c -- '--cache-to' "$write_log")" -eq 5
test "$(grep -c -- 'mode=max,ignore-error=true' "$write_log")" -eq 5
test "$(grep -c '^push' "$write_log")" -eq 5

off_log="$tmp_dir/off.log"
run_family off false false "$off_log" "$tmp_dir/off-summary.json"
test "$(grep -c -- '--cache-from' "$off_log" || true)" -eq 0
test "$(grep -c -- '--cache-to' "$off_log" || true)" -eq 0

echo "build image family registry cache checks passed"
