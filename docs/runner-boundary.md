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
GHCR writes in the separate `Publish Images` workflow. That workflow only runs
on exact release tags or explicit maintainer dispatch.
