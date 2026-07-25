# Image Contract

Every image is defined by `images/<name>/image.toml`.

Required fields:

```toml
schema = 1
name = "native-linux-x64"
contract_major = 1
platform = "linux-x64"
publish = true

[base]
image = "base-linux"

[runner]
profile = "kungfu-build-v4-linux-x64"
self_hosted_required = false

[build]
context = "."
dockerfile = "Dockerfile"
test_commands = [
  "python3 --version",
]
```

## Contract Identity

An image contract is identified by:

- image name;
- `contract_major`;
- platform;
- documented runner boundary;
- published digest.

Breaking changes should create a new `contract_major` or a new image name. A
release must not silently change the meaning of an existing image contract.

## Parent Images

Child images must reference parent images from this repository by manifest name.
Release automation should resolve parent image digests before building child
images.

The first graph is intentionally shallow:

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

## Test Commands

`test_commands` are cheap smoke commands that prove the image contract is
present. They are not consumer release builds.

Consumer repositories own their package-specific build commands.

`kungfu-native-linux-x64` is the maintained exception that also ships a
repository-owned consumer entrypoint. That entrypoint welds the exact Kungfu
source revision, `--no-optional` install, libnode platform seed, writable cache
layout, build-job cap, source-fallback Shifu path, and machine-readable build
receipt needed to prove the image's source-build contract. It stops before any
timed host-native workload.

`comparator-formal-runner` is a distribution envelope, not the measured
container. Consumers extract `/opt/formal-performance` from an exact image
digest and run the host CLI without a Docker socket mount. Its v1 contract
freezes the 20260719 balanced schedule, common resource ceilings, cgroup-v2
counter units, offline fresh-project timer, Kungfu/Aeron matched lane, and
fail-closed offline verifier. It can report descriptive medians, tails, and
ratios but must keep `winner_authority=false`, must not self-grade blinded M3/M4
reviews, and must not give Aeron a complete-product verdict.

`opencode-ci` is an Agent runtime envelope, not a model or credential image. Its
v1 contract pins OpenCode, runs as the unprivileged `kungfu` user, accepts only
runtime-supplied endpoint and model coordinates, emits JSONL evidence, and
requires an independent verifier. OpenCode text and exit status alone never
settle the job.

## First Publish Lock State

`images.lock.json` normally records every published image digest. A new image may
temporarily set `lock.status = "pending-first-publish"` in its manifest while it
is waiting for the first Buildchain publish transaction to produce an immutable
GHCR digest. After that release, the lock file should be updated with the
published digest so consumer smoke can pull the new image by digest. Once an
image participates in selective publication, its lock entry also records the
normalized OCI platform, contract major, parent digest, immutable content
coordinate, current release coordinate, and exact manifest smoke commands.
These fields form one closed family: partial provenance is rejected instead of
being treated as reusable content.
