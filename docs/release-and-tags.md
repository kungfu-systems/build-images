# Release And Tags

Build images use the repository release version as the first versioning layer.
Independent per-image semantic versions should be introduced only after usage
proves that the image family needs separate release cadence.

## Exact Tags

Exact repository tags map to exact image tags:

```text
repo:  v1.0.0-alpha.0
image: ghcr.io/kungfu-systems/build-images/base-linux:v1.0.0-alpha.0
image: ghcr.io/kungfu-systems/build-images/node24-pnpm:v1.0.0-alpha.0
image: ghcr.io/kungfu-systems/build-images/native-linux-x64:v1.0.0-alpha.0
```

Production releases use exact production tags:

```text
repo:  v1.0.0
image: ghcr.io/kungfu-systems/build-images/base-linux:v1.0.0
```

## Floating Tags

Floating tags are convenience tags only. Consumers that require reproducibility
should pin immutable digests.

Recommended floating tags after the release loop is stable:

```text
v1
v1.0
v1.0-alpha
```

## Digest Evidence

Every publish run must produce a digest summary containing:

- image name;
- image tag;
- contract major;
- platform;
- parent image digest when applicable;
- published digest;
- source commit;
- Buildchain release tag.

The digest summary is the rollback and audit anchor.

## Rollback

Rollback for a consumer should mean switching the consumer workflow back to the
previous exact digest or back to its non-container build path. A consumer should
not depend on deleting or mutating a published image tag.

