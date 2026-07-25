# demo-renderer

`demo-renderer` is a consumer-neutral, non-root media renderer for auditable
software demonstrations. It renders reviewed presentation contracts around a
complete transcript; it does not execute a product command, fetch application
data, or claim that the styled terminal is a literal operating-system capture.

The authoritative qualification architecture is `linux/amd64`. Consumers pin
the public image by immutable OCI digest and stage three read-only inputs:

- a `build-images.demo-scene/v1` scene;
- the complete UTF-8 product transcript;
- a `build-images.demo-projection/v1` projection whose one-based line
  references are validated against that transcript.

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
    --output /output \
    --renderer-image ghcr.io/kungfu-systems/build-images/demo-renderer@sha256:<accepted-digest>
```

Runtime network access is unnecessary and should be disabled by the caller.
Chromium requests are also denied inside the renderer. The container has no
Docker socket, host Home, signing material, credentials, or writable source
checkout. Only the declared read-only inputs and bounded output/tmpfs mounts
are needed.

The renderer fixes locale, timezone, viewport, frame rate, font family,
single-thread codec settings, volatile media metadata, and output ordering.
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
