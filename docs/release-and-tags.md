---
status: active
period: ongoing
theme: build-images-v4-publication
doc_type: technical-reference
source_level: local-files
confidence: high
sensitivity: public
evidence_grade: B
review_state: unreviewed
last_reviewed: 2026-09-06
ai_provenance:
  model_family: GPT-6
  product: Codex
  generated_at: 2026-09-06
  invisible_context_boundary: Describes tracked publication contracts; does not assert a completed release.
---

# Release And Tags

Build images use the repository release version as the first versioning layer.
Independent per-image semantic versions should be introduced only after usage
proves that the image family needs separate release cadence.

## Buildchain Governance

Buildchain v4 is the alpha release authority for this repository. Changes land
on `dev/v1/v1.3`, then a protected pull request targets `alpha/v1/v1.3`.
`Verify` retains the required `check` status, while `Build` seals the complete
OCI image family before any package write authority is available.

Tracked callers use `@v4-alpha` with the alpha contract lock. The stable v4
contract lock is retained for production channel adoption. Temporary train
validation uses only the trusted dispatch input; no train or exact runtime SHA
is persisted in workflow source.

The candidate contains the image layout, per-image smoke evidence, selective
build plan, and `oci-family.json`. Built images bind the candidate source and
version; reused images retain their original content provenance and digest.
All images use normalized OCI platform names. Shared layout blobs are
deduplicated, and new images are compressed for artifact transport.

After protected alpha Verify succeeds, the built-in v4 OCI provider checks the
complete family, publishes only missing exact tags, and anonymously reads back
every digest. A conflicting tag fails closed. Only complete public readback
allows release Git refs and GitHub Release evidence to converge. No consumer
publish shell hook is configured. Manual promotion is dry-run only.

The promotion caller explicitly grants `packages: write`; candidate builds do
not. Existing GHCR packages must allow repository-token writes and public pulls.
The provider records public readback without changing package visibility.

## Exact Tags

Exact repository tags map to exact image tags:

```text
repo:  v1.0.0-alpha.0
image: ghcr.io/kungfu-systems/build-images/base-linux:v1.0.0-alpha.0
image: ghcr.io/kungfu-systems/build-images/kungfu-verify:v1.0.0-alpha.0
image: ghcr.io/kungfu-systems/build-images/comparator-formal-runner:v1.3.0
image: ghcr.io/kungfu-systems/build-images/kungfu-native-linux-x64:v1.3.0-alpha.0
image: ghcr.io/kungfu-systems/build-images/node24-pnpm:v1.0.0-alpha.0
image: ghcr.io/kungfu-systems/build-images/latex-pdf-builder:v1.2.0-alpha.0
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

Each image record also distinguishes immutable content provenance from the
current release binding:

- `action` is `built` or `reused`;
- `content` identifies the version, exact ref, source SHA, and material SHA that
  produced the image bytes;
- `release` identifies the current version, exact ref, target ref, source SHA,
  and material SHA that publishes those bytes in the current family;
- `verification` binds the anonymous public manifest digest, normalized OCI
  platform, contract major, parent digest, and passed manifest smoke policy.

The digest summary is the rollback and audit anchor.

The GitHub Release contains `oci-family.json`, the per-image smoke JSON files,
`image-publish-plan.json`, and the provider's `oci-publication-readback.json`.
These preserve the candidate bytes and distinguish public publication from a
successful local build.

The schema-1 digest evidence and accepted `images.lock.json` formats remain
unchanged. After reading the exact release tag and publisher runtime from the
successful protected run, project the public v4 evidence into that retained
format:

```bash
python3 scripts/project-oci-evidence.py \
  --readback oci-publication-readback.json --family oci-family.json \
  --release-sha RELEASE_SHA --tooling-sha PUBLISHER_RUNTIME_SHA \
  --target-ref alpha/v1/v1.3 --output evidence.json
python3 scripts/accept-image-summary.py \
  --evidence evidence.json \
  --publish-run https://github.com/kungfu-systems/build-images/actions/runs/RUN_ID \
  --output images.lock.json
```

Projection rejects missing, private, conflicting, or mismatched image records.
Review the resulting lock through the normal development PR flow. The accepted
lock remains a reviewed consumer input. Historical v2 `evidence.json` assets
remain readable through the same acceptance command.

## Selective Build Planner

The DAG resolver can produce a fail-closed build selection from changed paths:

```bash
python3 scripts/resolve-image-dag.py --json \
  --changed-path images/node24-pnpm/Dockerfile
```

An image context change selects that image plus its downstream closure. A base
context change therefore selects the full family, while a LaTeX-only context
change selects only `latex-pdf-builder`. Image manifests, Buildchain metadata,
publisher/provenance code, workflows, image locks, empty baselines, and unknown
paths conservatively select the full family. Every selected image includes a
machine-readable direct, downstream, or global reason.

The candidate builder uses the last commit that changed `images.lock.json` as
the reviewed baseline. It proves that the lock's release source is an ancestor,
allows only generated version-state and KFD/lock acceptance changes between the
release source and that review point, then plans changes from the review point
to the current source. Generated `.buildchain/kfd/` evidence is image-neutral
after that reviewed anchor; other Buildchain metadata remains a global
invalidator. A proven empty delta reuses the whole family, while an empty path
input without Git proof remains a fail-closed full build. Version-only changes
in `package.json` and the Buildchain impact ledger do not rebuild image content;
any other unknown path remains a global invalidator.

Selected images and their downstream closure are built. Every other image is
reused only when the lock has a complete public digest, platform, contract
major, parent digest, smoke policy, content coordinate, and release coordinate.
An incomplete or inconsistent baseline converts the whole plan to a full build.
Reuse verifies the accepted digest anonymously, exports its exact manifest
bytes, and reruns the image smoke policy. The provider later creates the new
exact tag and verifies public readback for built and reused members alike.

The first release after publisher/provenance code changes is intentionally a
full-family build because those files are global invalidators. A later
image-scoped release is the selective canary.

## Publish Path

Image publishing is intentionally separated from normal pull request
verification.

- Pull requests use the reusable Buildchain `Verify` workflow and do not
  receive package write permission. The final `check` job preserves the status
  context required by protected channel branches.
- Feature branches merge to the active `dev/vN/vN.M` branch first. Buildchain
  alpha promotion is then triggered by a protected pull request from
  `dev/vN/vN.M` to `alpha/vN/vN.M`; do not merge feature branches directly into
  `alpha/*`.
- Buildchain promotion creates exact release tags such as `v1.0.0-alpha.0` or
  `v1.0.0` only after image evidence validates.
- Alpha pull requests run `Build` to produce the sealed OCI candidate.
  `Buildchain Ref Promotion` uses `publish-artifact-kind: oci` to publish it.
- Maintainers may run `Publish Images` manually with `publish=false` for a dry
  build, but manual publishing is rejected.
- The promotion workflow runs on GitHub-hosted `ubuntu-24.04` and uses
  `GITHUB_TOKEN` for GHCR writes.
- Published GHCR packages are required to be public. The organization Packages
  policy must allow public package creation and avoid forcing private defaults.
  Docker push cannot declare package visibility, so the publish workflow fails
  if the exact tag cannot be resolved through the anonymous GHCR pull path.
- Consumer smoke intentionally pulls the locked images without GHCR login. This
  keeps the public-consumption contract covered by CI instead of relying only on
  package settings in the GitHub UI.

## Rollback

Rollback for a consumer should mean switching the consumer workflow back to the
previous exact digest or back to its non-container build path. A consumer should
not depend on deleting or mutating a published image tag.

## Welded Surfaces

Version verdicts for this repository follow
[KFD-1](https://github.com/kungfu-systems/kfd/blob/dev/v1/v1.0/decisions/kfd-0001-release-versioning.md):
breaking a registered surface forces a major; additively evolving one or
adding one opens a minor; a change touching no registered surface is a patch
regardless of size. The register:

| ID | Surface | Kind | Where it is specified |
|---|---|---|---|
| `image-contracts` | per-image contract identity (`name + contract_major + platform + runner boundary + guaranteed tools`) | integration | [`image-contract.md`](image-contract.md) |
| `image-family` | the family member set (which images exist; parent references by name) | integration | [`image-contract.md`](image-contract.md) |
| `tag-scheme` | tag naming (exact image tags mirror exact repository tags) | integration | this document |
| `digest-summary-schema` | the digest summary produced by every publish run | cross-time | this document |

Consequences worth spelling out: refreshing image contents within a contract
(tool patch versions, security rebuilds) is a patch; adding a new image to the
family is a minor (floating consumers can learn from the `vX.Y` coordinate
that the image exists on that line); changing an existing image's contract
semantics, retiring an image, or changing the tag or digest-summary schema is
a major. Breaking changes to one image should normally be rerouted by minting
a new image name or `contract_major` (per `image-contract.md`), which keeps
the family change additive.

## Decision Log

Line openings (minor/major), register changes, and deprecations are recorded
here, newest first. Patches are intentionally absent.

| Date | Action | Line | Faces | Class | Rationale | PR |
|---|---|---|---|---|---|---|
| 2026-07-31 | extend | v1.3 | image-contracts | additive minor | Extend `demo-renderer` with source-resolution MP4/WebM plus deterministic 1280x720 MP4/WebM/GIF renditions encoded from one frame set, preserving one capture and renderer authority chain. | #338 |
| 2026-07-25 | add | v1.3 | image-family, image-contracts | additive minor | Add the consumer-neutral `demo-renderer` image with a digest-pinned browser/media/font toolchain and deterministic transcript-bound output contract. | #319 |
| 2026-07-25 | open | v1.3 | image-family, image-contracts | additive minor | Add `opencode-ci` as a pinned non-root OpenCode runtime with an external local-model and independent-verifier boundary. | #308 |
| 2026-07-19 | extend | v1.3 | image-family, image-contracts | additive minor | Add `comparator-formal-runner` as the exact-digest distribution and offline-verification contract for symmetric full-stack and Kungfu/Aeron performance evidence. | #248 |
| 2026-07-19 | open | v1.3 | image-family, image-contracts | additive minor | Add `kungfu-native-linux-x64` as the exact-digest Kungfu source-build environment with a fresh-consumer evidence contract. | #239 |
| 2026-07-09 | open | v1.2 | image-family, image-contracts | additive minor | Add `latex-pdf-builder` as a pnpm-driven publication PDF builder image rooted in the existing Kungfu image family. | #38 |
| 2026-07-02 | register | — | image-contracts, image-family, tag-scheme, digest-summary-schema | additive | Initial register established on adopting KFD-1 | — |
