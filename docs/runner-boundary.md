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
only after protected alpha verification succeeds. Its built-in v4 OCI provider
publishes the sealed candidate and anonymously verifies every image digest
before public release refs move. Candidate Build jobs have no packages write
permission.
`Publish Images` remains a manual dry-build diagnostic surface and rejects
manual pushes.
