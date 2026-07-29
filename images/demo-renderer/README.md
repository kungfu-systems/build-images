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
content-addressed PTY observation: fixed dimensions, at most 60 seconds,
10,000 events, and 4 MiB of canonical base64 terminal bytes. The renderer
replays those bytes through the pinned `@xterm/headless` state machine and
binds the capture root in its manifest. It does not copy raw capture bytes into
the media output.

The capture schema requires an empty authority-grant list. First-party or
System identity, KFD compliance, Product System metadata, package metadata,
scan output, registry history, or standalone generation cannot authorize the
render or publication. Those decisions remain outside the renderer and must be
bound by the caller's exact Work or Warrant, capability grant, runtime
isolation, Gate, and Release Passport.

The command writes a complete transcript, normalized scene and projection,
poster, MP4, WebM, README-compatible GIF, media probe, content manifest, and
checksums into an initially empty output directory:

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
    --output /output \
    --renderer-image ghcr.io/kungfu-systems/build-images/demo-renderer@sha256:<accepted-digest>
```

Runtime network access is unnecessary and should be disabled by the caller.
Chromium requests are also denied inside the renderer. The container has no
Docker socket, host Home, signing material, credentials, or writable source
checkout. Only the declared read-only inputs and bounded output/tmpfs mounts
are needed.

The renderer fixes locale, timezone, viewport, frame rate, font family,
terminal emulator version, single-thread codec settings, volatile media
metadata, and output ordering.
Its smoke renders the same fixture twice and requires byte-identical outputs.
Consumers still bind the exact image digest and architecture in their own gate
receipt; a different digest is a different renderer identity.

Pull requests that change the renderer run the pinned
`Demo Renderer Qualification` workflow on a GitHub-hosted `linux/amd64`
runner. It uploads one content-addressed evidence artifact containing the
deterministic smoke provenance, SPDX SBOM, complete Trivy report, and exact
checksums. Image publication remains owned by the protected Buildchain release
transaction; the qualification artifact does not itself publish or promote an
OCI tag.
