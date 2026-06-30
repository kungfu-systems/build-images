# Kungfu Build Images

This repository is the source of truth for maintained Kungfu build container
images.

The images are runtime environment assets. They are versioned and released as a
coherent family through Buildchain-governed repository releases, then consumed by
trusted workflows by exact tag or immutable digest.

## Initial Image Family

```text
base-linux
  -> node24-pnpm
  -> native-linux-x64
```

- `base-linux` defines the common Linux build baseline.
- `node24-pnpm` adds Node.js 24 and pnpm for GitHub Action and JavaScript build
  surfaces.
- `native-linux-x64` adds common native build tooling for Linux x64 consumers.

Scenario images such as `libnode-linux-x64-build` should be added only after the
base publish loop is stable.

## Repository Contract

- Image metadata lives in `images/<name>/image.toml`.
- Dockerfiles live next to their manifest.
- The manifest graph is shallow and explicit.
- Child images must reference a known parent image from this repository.
- Release summaries must record every published digest.
- Consumers that require reproducibility should pin images by digest.

## Local Verification

```bash
python3 scripts/verify-image-manifests.py
bash scripts/check-workflows.sh
```

Or run the repository check:

```bash
pnpm run check
```

The default verification path does not publish images and does not require a
self-hosted runner.

## Release Model

The repository uses one Buildchain release version for the image family at
first. Exact image tags mirror exact repository tags, for example:

```text
ghcr.io/kungfu-systems/build-images/base-linux:v1.0.0
ghcr.io/kungfu-systems/build-images/node24-pnpm:v1.0.0
ghcr.io/kungfu-systems/build-images/native-linux-x64:v1.0.0
```

See `docs/release-and-tags.md` for the tag and digest contract.

## Runner Boundary

The first release path should prefer GitHub-hosted runners for image build and
publish. Do not grant Docker group membership or sudo to an existing self-hosted
GitHub Actions runner service account.

See `docs/runner-boundary.md`.

