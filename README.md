# Kungfu Build Images

This repository is the source of truth for maintained Kungfu build container
images.

The images are runtime environment assets. They are versioned and released as a
coherent family through Buildchain-governed repository releases, then consumed by
trusted workflows by exact tag or immutable digest.

## Initial Image Family

```text
base-linux
  -> kungfu-verify
    -> comparator-formal-runner
    -> kungfu-native-linux-x64
    -> opencode-ci
  -> node24-pnpm
    -> latex-pdf-builder
  -> native-linux-x64
```

- `base-linux` defines the common Linux build baseline.
- `kungfu-verify` fixes the lightweight Kungfu CI entry tools used by
  `kungfu-code sync`, verify jobs, and publish preparation jobs.
- `comparator-formal-runner` distributes the frozen symmetric performance
  schedule, schemas, and offline verifier without putting a Docker socket or
  host tuning controls inside the image.
- `kungfu-native-linux-x64` adds the pinned GCC 14, Node 22, Python, Conan, and
  Rust toolchains required to build Kungfu native source reproducibly.
- `opencode-ci` adds a pinned non-root OpenCode runtime with an external
  local-model boundary and independent verification contract.
- `node24-pnpm` adds Node.js 24 and pnpm for GitHub Action and JavaScript build
  surfaces.
- `latex-pdf-builder` adds LaTeX PDF publication tooling while preserving pnpm
  build orchestration.
- `native-linux-x64` adds common native build tooling for Linux x64 consumers.

Native Kungfu build images should layer on top of `kungfu-verify` when their
contract needs the same Buildchain entry tools plus C++/Conan/CMake tooling.

## Repository Contract

- Image metadata lives in `images/<name>/image.toml`.
- Dockerfiles live next to their manifest.
- The manifest graph is shallow and explicit.
- Child images must reference a known parent image from this repository.
- Release summaries must record every published digest.
- Consumers that require reproducibility should pin images by digest.

## Local Verification

```bash
pnpm run check
```

The default verification path calls Buildchain's `build.yml@v2` channel router
with `buildchain-channel: auto`. Development and prerelease work uses
`v2-alpha` with `.buildchain/alpha-contract-lock.json`; stable release work uses
`v2` with `.buildchain/contract-lock.json`. The lifecycle validates KFD-1/2/3
release evidence, image manifests, the image lock, workflow syntax, and shell
syntax. It does not publish images and does not require a self-hosted runner.

The GitHub `Verify` workflow exposes a `check` job so Buildchain v2 promotion
can use it as the protected release-line status check.

## Release Model

The repository uses one Buildchain release version for the image family at
first. Buildchain v2 owns channel promotion, image publish transactions, durable
publish evidence, and exact release tags. Exact image tags mirror exact
repository tags, for example:

```text
ghcr.io/kungfu-systems/build-images/base-linux:v1.0.0
ghcr.io/kungfu-systems/build-images/kungfu-verify:v1.0.0
ghcr.io/kungfu-systems/build-images/comparator-formal-runner:v1.3.0
ghcr.io/kungfu-systems/build-images/kungfu-native-linux-x64:v1.3.0-alpha.0
ghcr.io/kungfu-systems/build-images/node24-pnpm:v1.0.0
ghcr.io/kungfu-systems/build-images/latex-pdf-builder:v1.2.0-alpha.0
ghcr.io/kungfu-systems/build-images/native-linux-x64:v1.0.0
```

Release publication becomes selective only after a reviewed,
provenance-complete `images.lock.json` exists. The changed image plus its
downstream DAG closure is built; unchanged members are retagged from immutable
accepted digests, publicly verified, and smoked again. Missing provenance or
any unknown/global change fails closed to a full-family build. The Release
Passport still contains the complete manifest-declared family and records
`built` versus `reused` content truthfully.

See `docs/release-and-tags.md` for the tag and digest contract.

## Runner Boundary

The first release path should prefer GitHub-hosted runners for image build and
publish. Do not grant Docker group membership or sudo to an existing self-hosted
GitHub Actions runner service account.

See `docs/runner-boundary.md`.

## Comparator Pilot Environments

Disposable, immutable-input Aeron, ClickHouse, PostgreSQL, and fail-closed
Kungfu comparator environments live in [`pilots/comparator`](pilots/comparator/README.md).
They provide unscored functional and recovery-path evidence only; Docker is not
the authority for the native performance comparison. The separately versioned
`comparator-formal-runner` evidence class keeps raw symmetric measurements
machine-verifiable while forcing `winner_authority=false`.
