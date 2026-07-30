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

render "$scratch/first"
render "$scratch/second"
render_capture "$scratch/capture-first" "$fixture_root/terminal-capture.json"
render_capture "$scratch/capture-second" "$fixture_root/terminal-capture.json"

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
diff -u "$scratch/capture-first/checksums.sha256" "$scratch/capture-second/checksums.sha256"
cmp "$scratch/capture-first/manifest.json" "$scratch/capture-second/manifest.json"

ffmpeg -hide_banner -loglevel error \
  -i "$scratch/capture-first/poster.png" \
  -f rawvideo -pix_fmt rgb24 \
  "$scratch/capture-poster.rgb"

python3 - "$scratch/capture-poster.rgb" <<'PY'
import pathlib, sys

pixels = pathlib.Path(sys.argv[1]).read_bytes()
assert len(pixels) == 640 * 360 * 3

def nearby(target, tolerance=8):
    return sum(
        1
        for index in range(0, len(pixels), 3)
        if all(abs(pixels[index + channel] - target[channel]) <= tolerance for channel in range(3))
    )

# xterm 256-color background 17 and the explicit RGB foreground are both
# present in the poster. A text-only replay or a one-color CSS fallback fails.
assert nearby((0, 0, 95), tolerance=2) > 100
assert nearby((103, 232, 165)) > 5
PY

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
assert manifest["renderer"]["contractVersion"] == "1.2.0"
assert "terminal-capture.json" not in manifest["outputs"]
PY

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
