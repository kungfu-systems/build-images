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

render_capture() {
  output="$1"
  capture="$2"
  mkdir "$output"
  demo-renderer \
    --scene "$fixture_root/scene.json" \
    --transcript "$fixture_root/transcript.txt" \
    --projection "$fixture_root/projection.json" \
    --terminal-capture "$capture" \
    --output "$output" \
    --renderer-image "local-smoke@sha256:0000000000000000000000000000000000000000000000000000000000000000"
}

render_native() {
  output="$1"
  mkdir "$output"
  demo-renderer \
    --scene "$fixture_root/scene.json" \
    --transcript "$fixture_root/complete-transcript.txt" \
    --projection "$fixture_root/public-projection.json" \
    --terminal-capture "$fixture_root/terminal-capture.json" \
    --rendition-set "$fixture_root/rendition-set.json" \
    --output "$output" \
    --renderer-image "local-smoke@sha256:0000000000000000000000000000000000000000000000000000000000000000"
}

render "$scratch/first"
render "$scratch/second"
render_capture "$scratch/capture-first" "$fixture_root/terminal-capture.json"
render_capture "$scratch/capture-second" "$fixture_root/terminal-capture.json"
render_native "$scratch/native-first"
render_native "$scratch/native-second"

for member in \
  complete-transcript.txt \
  public-projection.json \
  scene.json \
  poster.png \
  demo.mp4 \
  demo.webm \
  demo-720p.mp4 \
  demo-720p.webm \
  demo.gif \
  media-probe.json \
  manifest.json \
  checksums.sha256
do
  test -s "$scratch/first/$member"
done

diff -u "$scratch/first/checksums.sha256" "$scratch/second/checksums.sha256"
cmp "$scratch/first/manifest.json" "$scratch/second/manifest.json"
diff -u "$scratch/capture-first/checksums.sha256" "$scratch/capture-second/checksums.sha256"
cmp "$scratch/capture-first/manifest.json" "$scratch/capture-second/manifest.json"
diff -u "$scratch/native-first/checksums.sha256" "$scratch/native-second/checksums.sha256"
cmp "$scratch/native-first/manifest.json" "$scratch/native-second/manifest.json"

ffmpeg -hide_banner -loglevel error \
  -i "$scratch/capture-first/poster.png" \
  -f rawvideo -pix_fmt rgb24 \
  "$scratch/capture-poster.rgb"

python3 - "$scratch/capture-poster.rgb" <<'PY'
import pathlib, sys

pixels = pathlib.Path(sys.argv[1]).read_bytes()
assert len(pixels) == 1920 * 1080 * 3

def nearby(target, tolerance=8):
    return sum(
        1
        for index in range(0, len(pixels), 3)
        if all(abs(pixels[index + channel] - target[channel]) <= tolerance for channel in range(3))
    )

def nearby_at(target, x_min, y_min, tolerance=8):
    for y in range(y_min, 1080):
        for x in range(x_min, 1920):
            index = (y * 1920 + x) * 3
            if all(abs(pixels[index + channel] - target[channel]) <= tolerance for channel in range(3)):
                return True
    return False

# xterm 256-color background 17 and the explicit RGB foreground are both
# present in the poster. A text-only replay or a one-color CSS fallback fails.
assert nearby((0, 0, 95), tolerance=2) > 100
assert nearby((103, 232, 165)) > 5
# The capture fixture writes one explicit RGB cell at PTY row 36, column 150.
# It must land near the lower-right of the 1080p terminal. This prevents a
# 720p-sized terminal grid from being embedded inside a 1080p frame.
assert nearby_at((103, 232, 165), x_min=1700, y_min=780)
PY

python3 - "$scratch/first/media-probe.json" <<'PY'
import json, sys
probe = json.load(open(sys.argv[1], encoding="utf-8"))
assert probe["schema"] == "build-images.demo-media-probe/v1"
assert probe["passed"] is True
assert {item["name"] for item in probe["media"]} == {
    "demo.mp4",
    "demo.webm",
    "demo-720p.mp4",
    "demo-720p.webm",
    "demo.gif",
    "poster.png",
}
for item in probe["media"]:
    if item["name"] in {"demo-720p.mp4", "demo-720p.webm", "demo.gif"}:
        assert item["width"] == 1280
        assert item["height"] == 720
    else:
        assert item["width"] == 1920
        assert item["height"] == 1080
    assert item["bytes"] > 0
PY

python3 - "$scratch/capture-first/manifest.json" <<'PY'
import json, sys
manifest = json.load(open(sys.argv[1], encoding="utf-8"))
capture = manifest["inputs"]["terminalCapture"]
assert capture["schema"] == "kungfu.terminal-capture/v1"
assert capture["events"] == 2
assert capture["bytes"] > 0
assert manifest["policy"]["runtimeTextAuthority"] == "terminal-capture.json"
assert manifest["policy"]["visualClassification"] == "bounded-pty-replay"
assert manifest["renderer"]["terminal"]["engine"] == "@xterm/headless"
assert manifest["renderer"]["terminal"]["version"] == "5.5.0"
assert manifest["renderer"]["terminal"]["inventoryRoot"].startswith("sha256:")
assert manifest["renderer"]["terminal"]["styleModel"] == "ansi16-xterm256-rgb/v1"
assert manifest["renderer"]["contractVersion"] == "1.3.0"
assert manifest["derivation"]["policy"] == "single-frame-set-deterministic-renditions/v1"
assert manifest["derivation"]["sourceFrames"]["width"] == 1920
assert manifest["derivation"]["sourceFrames"]["height"] == 1080
assert manifest["derivation"]["renditions"]["demo.mp4"]["operation"] == "source-frame-encode"
assert manifest["derivation"]["renditions"]["demo-720p.mp4"] == {
    "height": 720,
    "operation": "lanczos-downscale-from-source-frames",
    "width": 1280,
}
assert "terminal-capture.json" not in manifest["outputs"]
PY

python3 - "$scratch/native-first/manifest.json" <<'PY'
import json, sys
manifest = json.load(open(sys.argv[1], encoding="utf-8"))
assert manifest["policy"]["runtimeTextAuthority"] == "rendition-set.json"
assert manifest["derivation"]["policy"] == "independent-native-frame-sets/v1"
sets = manifest["derivation"]["sourceFrameSets"]
assert [(item["role"], item["width"], item["height"]) for item in sets] == [
    ("primary", 1920, 1080),
    ("responsive", 1280, 720),
]
assert sets[0]["captureRoot"] != sets[1]["captureRoot"]
assert manifest["inputs"]["renditions"][0]["terminalCapture"]["dimensions"] == {
    "columns": 150,
    "rows": 36,
}
assert manifest["inputs"]["renditions"][1]["terminalCapture"]["dimensions"] == {
    "columns": 100,
    "rows": 28,
}
for name, rendition in manifest["derivation"]["renditions"].items():
    assert rendition["operation"] == "native-frame-set-encode", (name, rendition)
PY

ffmpeg -hide_banner -loglevel error -y \
  -i "$scratch/native-first/demo.mp4" -frames:v 1 \
  -vf scale=1280:720:flags=neighbor "$scratch/native-primary-scaled.png"
ffmpeg -hide_banner -loglevel error -y \
  -i "$scratch/native-first/demo-720p.mp4" -frames:v 1 \
  "$scratch/native-responsive.png"
if cmp -s "$scratch/native-primary-scaled.png" "$scratch/native-responsive.png"; then
  echo "native 720p content unexpectedly equals a scaled 1080p frame" >&2
  exit 1
fi

node - "$fixture_root/terminal-capture.json" "$scratch/implicit-grant.json" <<'JS'
const fs = require('node:fs');
const capture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
capture.authority.grants = ['implicit-system-authority'];
fs.writeFileSync(process.argv[3], `${JSON.stringify(capture, null, 2)}\n`);
JS
mkdir "$scratch/rejected"
if demo-renderer \
  --scene "$fixture_root/scene.json" \
  --transcript "$fixture_root/transcript.txt" \
  --projection "$fixture_root/projection.json" \
  --terminal-capture "$scratch/implicit-grant.json" \
  --output "$scratch/rejected" \
  --renderer-image "local-smoke@sha256:0000000000000000000000000000000000000000000000000000000000000000"
then
  echo "terminal capture with an implicit authority grant was accepted" >&2
  exit 1
fi

node - "$fixture_root/terminal-capture.json" "$scratch/legacy-passed.json" <<'JS'
const fs = require('node:fs');
const capture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
capture.completion.status = 'passed';
fs.writeFileSync(process.argv[3], `${JSON.stringify(capture, null, 2)}\n`);
JS
mkdir "$scratch/rejected-passed"
if demo-renderer \
  --scene "$fixture_root/scene.json" \
  --transcript "$fixture_root/transcript.txt" \
  --projection "$fixture_root/projection.json" \
  --terminal-capture "$scratch/legacy-passed.json" \
  --output "$scratch/rejected-passed" \
  --renderer-image "local-smoke@sha256:0000000000000000000000000000000000000000000000000000000000000000"
then
  echo "terminal capture with the legacy passed sentinel was accepted" >&2
  exit 1
fi
