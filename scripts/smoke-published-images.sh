#!/bin/bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
lock_path="$repo_root/images.lock.json"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required for published image smoke" >&2
  exit 1
fi

image_count="$(python3 - "$lock_path" <<'PY'
import json, sys
print(len(json.load(open(sys.argv[1], encoding="utf-8"))["images"]))
PY
)"

i=0
while [ "$i" -lt "$image_count" ]; do
  image_ref="$(python3 - "$lock_path" "$i" <<'PY'
import json, sys
entry = json.load(open(sys.argv[1], encoding="utf-8"))["images"][int(sys.argv[2])]
print(f"{entry['image']}@{entry['digest']}")
PY
)"
  image_name="$(python3 - "$lock_path" "$i" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["images"][int(sys.argv[2])]["name"])
PY
)"

  echo "::group::pull ${image_ref}"
  docker pull "$image_ref"
  echo "::endgroup::"

  test_count="$(python3 - "$lock_path" "$i" <<'PY'
import json, sys
entry = json.load(open(sys.argv[1], encoding="utf-8"))["images"][int(sys.argv[2])]
print(len(entry["test_commands"]))
PY
)"
  j=0
  while [ "$j" -lt "$test_count" ]; do
    test_command="$(python3 - "$lock_path" "$i" "$j" <<'PY'
import json, sys
entry = json.load(open(sys.argv[1], encoding="utf-8"))["images"][int(sys.argv[2])]
print(entry["test_commands"][int(sys.argv[3])])
PY
)"
    echo "::group::smoke ${image_name}: ${test_command}"
    docker run --rm "$image_ref" sh -lc "$test_command"
    echo "::endgroup::"
    j=$((j + 1))
  done
  i=$((i + 1))
done

