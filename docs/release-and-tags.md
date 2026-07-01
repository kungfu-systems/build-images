# Release And Tags

Build images use the repository release version as the first versioning layer.
Independent per-image semantic versions should be introduced only after usage
proves that the image family needs separate release cadence.

## Buildchain Governance

Buildchain v2 is the release authority for this repository. Day-to-day changes
land on `dev/vN/vN.M`, reviewed channel promotion moves through
`alpha/vN/vN.M` and `release/vN/vN.M`, and Buildchain creates the exact
version-state commits plus exact/floating tags.

Image publishing is a Buildchain publish transaction. The promotion workflow
creates or resumes the release transaction, builds or reuses exact OCI image
tags, writes publish evidence, and only then lets Buildchain move exact and
floating Git refs.

The publish command verifies that:

- every exact image tag is either newly pushed or already present with matching
  Buildchain version/material labels;
- published packages are public and anonymously pullable from GHCR;
- `BUILDCHAIN_PUBLISH_EVIDENCE` contains every image digest before public Git
  refs move;
- manual workflow dispatch cannot push images.

This keeps Docker credentials and Docker permissions inside the governed
promotion job while preserving Buildchain's durable rerun/repair state.

## Exact Tags

Exact repository tags map to exact image tags:

```text
repo:  v1.0.0-alpha.0
image: ghcr.io/kungfu-systems/build-images/base-linux:v1.0.0-alpha.0
image: ghcr.io/kungfu-systems/build-images/kungfu-verify:v1.0.0-alpha.0
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

The Buildchain publish command stores this summary next to
`BUILDCHAIN_PUBLISH_EVIDENCE`; Buildchain persists the evidence into the durable
release-state ref for fresh-runner reruns.

The checked-in `images.lock.json` records the latest accepted alpha image
digests for consumer smoke. This lock file is intentionally separate from the
publish artifact: it is a reviewed consumer input, not a byproduct of the
publishing job.

## Publish Path

Image publishing is intentionally separated from normal pull request
verification.

- Pull requests use the `Verify` workflow and do not receive package write
  permission.
- Feature branches merge to the active `dev/vN/vN.M` branch first. Buildchain
  alpha promotion is then triggered by a protected pull request from
  `dev/vN/vN.M` to `alpha/vN/vN.M`; do not merge feature branches directly into
  `alpha/*`.
- Buildchain promotion creates exact release tags such as `v1.0.0-alpha.0` or
  `v1.0.0` only after image evidence validates.
- The image publish command runs inside `Buildchain Ref Promotion` with
  `publish-transaction: "true"`.
- Maintainers may run `Publish Images` manually with `publish=false` for a dry
  build, but manual publishing is rejected.
- The promotion workflow runs on GitHub-hosted `ubuntu-24.04` and uses
  `GITHUB_TOKEN` for GHCR writes.
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
