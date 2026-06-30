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

The `Publish Images` workflow uploads this summary as an artifact named
`image-digests-<tag>`.

The checked-in `images.lock.json` records the latest accepted alpha image
digests for consumer smoke. This lock file is intentionally separate from the
publish artifact: it is a reviewed consumer input, not a byproduct of the
publishing job.

## Publish Path

Image publishing is intentionally separated from normal pull request
verification.

- Pull requests use the `Verify` workflow and do not receive package write
  permission.
- `Publish Images` runs on exact release tags such as `v1.0.0-alpha.0` or
  `v1.0.0`.
- Maintainers may also run `Publish Images` manually with `publish=false` for a
  dry build or `publish=true` for a trusted publish.
- The workflow runs on GitHub-hosted `ubuntu-24.04` and uses `GITHUB_TOKEN` for
  GHCR writes.
- Published GHCR packages are required to be public. The organization Packages
  policy must allow public package creation and avoid forcing private defaults.
  Docker push cannot declare package visibility, so the publish workflow fails
  after push if any package is not `public` or if the tag cannot be resolved
  through the anonymous GHCR pull path.
- Consumer smoke intentionally pulls the locked images without GHCR login. This
  keeps the public-consumption contract covered by CI instead of relying only on
  package settings in the GitHub UI.

## Rollback

Rollback for a consumer should mean switching the consumer workflow back to the
previous exact digest or back to its non-container build path. A consumer should
not depend on deleting or mutating a published image tag.
