# Runner Boundary

Image build and publish jobs are allowed to use Docker, but that permission must
stay inside a deliberate builder boundary.

## Defaults

- Prefer GitHub-hosted Linux runners for initial image build and publish.
- Do not publish on untrusted fork pull requests.
- Do not expose GHCR publish credentials to pull request jobs.
- Do not grant Docker group membership to an existing self-hosted runner service
  account.
- Do not grant sudo to a runner service account for image build convenience.

## Self-Hosted Builder Boundary

If a self-hosted image builder becomes necessary, create a separate design before
execution. That design should choose one of:

- rootless BuildKit under a dedicated builder account;
- a dedicated ephemeral builder host;
- a hosted builder service.

The existing general-purpose GitHub Actions runner should remain isolated from
Docker daemon control unless a separate security review explicitly changes that
boundary.

## Trusted Triggers

Publish jobs should be limited to trusted triggers such as protected branch
release paths, trusted tags, or explicit maintainer dispatches. Pull requests
from forks should only run read-only validation.

The repository implements this by keeping normal `Verify` read-only and placing
GHCR writes in the Buildchain promotion workflow. The promotion workflow runs
only after protected alpha/release verification succeeds, enables
`publish-transaction`, and writes evidence before public release refs move.
`Publish Images` remains a manual dry-build diagnostic surface and rejects
manual pushes.

## Registry Cache Boundary

Fresh builders may read the public per-image BuildKit cache stored beside each
GHCR image package. Cache refs use the form
`buildcache-c<contract-major>-linux-amd64-v1`, so image identity, platform, and
cache contract are isolated. BuildKit still keys records from the Dockerfile,
build context, build arguments, and resolved parent image; changing a parent
therefore cannot silently reuse a result built from the old parent.

Cache reads are an untrusted performance hint. A missing or unavailable cache
falls back to normal execution, and every result still runs the existing image
smoke before its exact release tag is pushed. Cache refs are mutable and are
never release evidence, provenance, rollback anchors, or consumer coordinates.

Only `scripts/publish-image-family.sh`, invoked by the protected Buildchain
promotion transaction after GHCR login, enables cache export. Normal Verify,
pull request, and manual dry-build paths remain cache-read-only. Cache export
uses `mode=max` and `ignore-error=true`; a cache service failure cannot replace
or bypass the exact-image push, public digest verification, publish evidence,
or Release Passport gates.
