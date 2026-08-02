# demo-renderer

`demo-renderer` is a consumer-neutral, non-root media renderer for auditable
software demonstrations. It renders reviewed presentation contracts around a
complete transcript; it does not execute a product command, fetch application
data, or grant execution or publication authority.

The authoritative qualification architecture is `linux/amd64`. Consumers pin
the public image by immutable OCI digest and stage three read-only inputs:

- a `build-images.demo-scene/v1` scene;
- the complete UTF-8 product transcript;
- a `build-images.demo-projection/v1` projection whose one-based line
  references are validated against that transcript.

Callers may additionally stage one `kungfu.terminal-capture/v1` file and pass
`--terminal-capture /input/terminal-capture.json`. The capture is a bounded,
content-addressed PTY observation: fixed dimensions, at most 60 seconds by
default, 10,000 events, and 4 MiB of canonical base64 terminal bytes. The renderer
replays those bytes through the pinned `@xterm/headless` state machine and
binds the capture root in its manifest. ANSI 16-color, xterm 256-color, RGB
foreground/background colors, inverse video, bold, dim, italic, underline,
strikethrough, overline, and invisible-cell state are replayed through a fixed
renderer-owned style model; raw SGR bytes never enter the page as markup. It
does not copy raw capture bytes into the media output.

Long demonstrations require the scene to declare `durationClass: long-form`.
That explicit class raises only that scene and its matching terminal capture to
a 180-second ceiling, lowers the frame-rate ceiling to 10 fps, and preserves the
1,800-frame bound that already limits a 60-second standard scene at 30 fps.
Omitting the class remains `standard`; it does not inherit long-form authority.
`demo-renderer --validate-only` exercises the same admission logic without
creating media and emits an observation-only validation result with no grants.

The completion sentinel accepts any bounded, versioned result schema with a
`qualified` status and exact report root. Product-specific identity does not
grant renderer authority and is not hard-coded into this image.

For native responsive media, callers stage a
`kungfu.auditable-demo.rendition-set/v1` plus two independently recorded
captures: a 1920x1080 primary scene with its own PTY dimensions and a 1280x720
responsive scene with different PTY dimensions. The renderer validates every
declared root, replays each capture into a separate Chromium viewport and
frame directory, and records both frame-set identities in the render manifest.
The responsive outputs are never derived by scaling the primary frames.

The capture schema requires an empty authority-grant list. First-party or
System identity, KFD compliance, Product System metadata, package metadata,
scan output, registry history, or standalone generation cannot authorize the
render or publication. Those decisions remain outside the renderer and must be
bound by the caller's exact Work or Warrant, capability grant, runtime
isolation, Gate, and Release Passport.

The command writes a complete transcript, normalized scene and projection,
source-resolution MP4/WebM/poster media, 1280x720 MP4/WebM responsive
renditions, a 1280x720 README-compatible GIF, a media probe, a content
manifest, and checksums into an initially empty output directory. Every media
member is encoded from its declared deterministic frame set. With a rendition
set, the 1080p and 720p members contain different terminal layouts because the
product was executed independently under different PTY dimensions.

Terminal chrome and PTY cells scale with the source scene. A 1920x1080 source
therefore uses the same composition as the 1280x720 responsive rendition
instead of embedding a 720p-sized terminal inside the larger frame. The smoke
fixture places a colored cell at PTY row 36, column 150 and verifies that it
reaches the lower-right region of the 1080p poster.

```bash
docker run --rm --network none --read-only \
  --user 1001:1001 \
  --tmpfs /tmp:rw,noexec,nosuid,size=512m \
  --mount type=bind,src="$PWD/evidence",dst=/input,readonly \
  --mount type=bind,src="$PWD/rendered",dst=/output \
  ghcr.io/kungfu-systems/build-images/demo-renderer@sha256:<accepted-digest> \
  demo-renderer \
    --scene /input/scene.json \
    --transcript /input/complete-transcript.txt \
    --projection /input/public-projection.json \
    --terminal-capture /input/terminal-capture.json \
    --rendition-set /input/rendition-set.json \
    --output /output \
    --renderer-image ghcr.io/kungfu-systems/build-images/demo-renderer@sha256:<accepted-digest>
```

Runtime network access is unnecessary and should be disabled by the caller.
Chromium requests are also denied inside the renderer. The container has no
Docker socket, host Home, signing material, credentials, or writable source
checkout. Only the declared read-only inputs and bounded output/tmpfs mounts
are needed.

The primary scene must be 1920x1080 and the responsive scene must be 1280x720
when a rendition set is supplied. The renderer fixes locale, timezone,
viewport, frame rate, font family, terminal emulator version, single-thread
codec settings, volatile media metadata, and output ordering.
Its smoke renders the same ANSI-colored fixture twice, requires byte-identical
outputs, and checks the poster pixels for the fixed palette and RGB colors.
Consumers still bind the exact image digest and architecture in their own gate
receipt; a different digest is a different renderer identity.

Pull requests that change the renderer run the pinned
`Demo Renderer Qualification` workflow on a GitHub-hosted `linux/amd64`
runner. It uploads one content-addressed evidence artifact containing the
deterministic smoke provenance, SPDX SBOM, complete Trivy report, and exact
checksums. Image publication remains owned by the protected Buildchain release
transaction; the qualification artifact does not itself publish or promote an
OCI tag.
