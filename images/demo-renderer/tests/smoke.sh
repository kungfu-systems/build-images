#!/bin/bash
set -euo pipefail

fixture_root="/opt/kungfu/demo-renderer/tests/fixtures"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

render() {
  output="$1"
  mkdir "$output"
  demo-renderer \
    --scene "$fixture_root/scene.json" \
    --transcript "$fixture_root/transcript.txt" \
    --projection "$fixture_root/projection.json" \
    --output "$output" \
    --renderer-image "local-smoke@sha256:0000000000000000000000000000000000000000000000000000000000000000"
}

render "$scratch/first"
render "$scratch/second"

for member in \
  complete-transcript.txt \
  public-projection.json \
  scene.json \
  poster.png \
  demo.mp4 \
  demo.webm \
  demo.gif \
  media-probe.json \
  manifest.json \
  checksums.sha256
do
  test -s "$scratch/first/$member"
done

diff -u "$scratch/first/checksums.sha256" "$scratch/second/checksums.sha256"
cmp "$scratch/first/manifest.json" "$scratch/second/manifest.json"

python3 - "$scratch/first/media-probe.json" <<'PY'
import json, sys
probe = json.load(open(sys.argv[1], encoding="utf-8"))
assert probe["schema"] == "build-images.demo-media-probe/v1"
assert probe["passed"] is True
assert {item["name"] for item in probe["media"]} == {"demo.mp4", "demo.webm", "demo.gif", "poster.png"}
for item in probe["media"]:
    assert item["width"] == 640
    assert item["height"] == 360
    assert item["bytes"] > 0
PY
