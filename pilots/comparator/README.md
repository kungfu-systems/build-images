# Comparator Docker Pilot Kit

This directory provides disposable, version-locked environments for qualifying
the user-visible functional and recovery paths used by the Kungfu comparator
program. It does **not** produce performance evidence.

## Profiles

| Profile | Lock | Current state | Pilot coverage |
| --- | --- | --- | --- |
| Aeron | 1.52.2 JAR + SHA-256; Temurin image digest | Ready | recorded publisher, forced driver restart, archive copy/restore entry |
| ClickHouse | 26.3.10.60 LTS image digest | Ready | create/insert/query, forced restart, Native export/import |
| PostgreSQL | 18.4 Bookworm image digest | Ready | create/insert/query, forced restart, `pg_dump`/restore |
| Kungfu | prebuilt CLI package + SHA-256 + source SHA + runtime image digest | Input required | Episode write/query, bundle export, forced restart, fsck, isolated restore |

The Kungfu pilot consumes `kungfu-episodes-cli-linux-x64.tar.gz` after another
job has built it. The Dockerfile verifies the supplied SHA-256 and
`kungfu.product.cli/v1` metadata, installs the package, and runs the CLI. It
does not check out or compile Kungfu source.

## Safe usage

Planning is read-only and is the default local entry:

```bash
bash pilots/comparator/scripts/pilot.sh plan clickhouse
```

Execution is explicit and intended for GitHub-hosted CI or another disposable
Docker host:

```bash
bash pilots/comparator/scripts/pilot.sh smoke clickhouse --execute
```

Each execution uses a unique Compose project and project-scoped named volumes.
The cleanup trap only runs `docker compose down --volumes --remove-orphans` for
that project. It does not use privileged mode, host networking, a Docker socket
mount, or global Docker cleanup.

Generated manifests and logs are stored under `.artifacts/` and say
`pilot_unscored: true`, `native_performance_authority: false`, and
`user_outcome_qualification_authority: false`. Docker results cannot replace
the designated native-host performance program, installation-cost study, or
the final user-perspective comparison protocol.

`environment-manifest.schema.json` is the machine-readable handoff contract for
each retained run manifest. `environment.lock.json` remains the immutable input
record for the profile definitions.

## Configuration slots

`realistic-default` is the only active slot. `expert-tuned` is reserved until a
separately reviewed product-specific tuning record exists. A tuned run must not
quietly replace the default user path.

## Kungfu package boundary

Package production and package consumption are deliberately separate:

1. An upstream job builds `kungfu-episodes-cli-linux-x64.tar.gz` and uploads it
   as an Actions artifact.
2. The reusable `Comparator Kungfu Package Smoke` workflow downloads that
   artifact, verifies its exact SHA-256, and places it in the Docker build
   context.
3. The package-consumer image validates `product.json`, resolves the declared
   CLI and compatibility entries, and runs the functional/recovery smoke.

The caller supplies the package artifact name, package version, exact package
SHA-256, exact source commit, and retained workflow evidence. These values are
written into the run manifest. The static environment lock intentionally does
not invent them before a package exists.

The smoke writes and seals an Episode, proves it through the query surface,
exports it, kills the container, checks the retained journal after restart,
and imports the bundle into a separate workspace.

The ordinary pull-request workflow keeps running the three self-contained
profiles. Kungfu joins only through the reusable package workflow because a
package must already exist in the caller's workflow run:

```yaml
jobs:
  comparator-kungfu:
    needs: build-kungfu-cli
    uses: kungfu-systems/build-images/.github/workflows/comparator-kungfu-package-smoke.yml@dev/v1/v1.2
    with:
      package_artifact_name: ${{ needs.build-kungfu-cli.outputs.artifact_name }}
      package_sha256: ${{ needs.build-kungfu-cli.outputs.package_sha256 }}
      package_version: ${{ needs.build-kungfu-cli.outputs.package_version }}
      source_sha: ${{ needs.build-kungfu-cli.outputs.source_sha }}
```

This Docker evidence remains explicitly unscored and non-authoritative for
performance. Installation-cost comparisons must use the agreed user delivery
path; neither upstream build time nor a prebuilt image's startup time should be
silently substituted for that measurement.

## Frozen-plan qualification

`comparator_qualification.py` is a separate evidence path for a narrower claim:
complete **containerized user-outcome qualification** from the same locked
Compose environment. It does not relabel ordinary smoke output. A production
plan must pin the charter, fixture set, `compose.yaml`, environment lock, runner
image, configuration slot, ordered scenario steps, oracles, repetition count,
timeouts, retained artifacts, and an allowlisted workload adapter by registry,
version, entrypoint, artifact SHA-256, semantics SHA-256, answer-free execution
fixture SHA-256, verifier-only oracle SHA-256, and job/tier mapping.

The comparator program outside this repository owns the frozen charter,
product-advocate review, report, scoring, and Phase B join. build-images owns
only reproducible execution and bundle integrity.

```bash
python3 pilots/comparator/scripts/comparator_qualification.py \
  validate-plan --plan /path/to/frozen-plan.json

python3 pilots/comparator/scripts/comparator_qualification.py \
  plan --plan /path/to/frozen-plan.json

python3 pilots/comparator/scripts/comparator_qualification.py \
  run --plan /path/to/frozen-plan.json --execute

python3 pilots/comparator/scripts/comparator_qualification.py \
  verify-bundle --bundle pilots/comparator/.artifacts/<destination>/<bundle-id>
```

`validate-plan` and `plan` do not start Docker. `run` requires `--execute`, uses
only this directory's `compose.yaml`, and delegates each production step to the
exact digest-locked adapter declared by `workload-adapters/registry.json`. The
adapter receives only the fixed runner arguments and executes its workload in
the locked Compose services; arbitrary commands are not accepted. Replacement
Compose files, registries, adapters, mismatched locks, unmapped job/tier labels,
unpinned images, `:latest`, fewer than three repetitions, missing oracles,
missing artifacts, and digest mismatches fail closed.

Each repetition retains step timing, process status, cleanup status, oracle
results, service logs, Docker/Compose and kernel facts, Compose resource
limits, container inspect data, CPU/memory observations, and SHA-256 records
for every raw artifact. Every tier event is followed by the same job-specific
facts query; the exact query result, tier postcondition, and derived verdict are
retained separately. The execution fixture contains no expected outcome. The
self-contained bundle also retains the exact Compose file, environment lock,
pilot adapter, qualification runner, workload adapter registry, adapter
artifact, semantic evaluator, execution fixture, verifier-only oracle, and
their digests. Every run binds the scenario, declared job, tier, action,
adapter inputs, observed facts, and tier evidence into SHA-256 receipts. The
offline verifier rejects relabelled evidence, adapter tampering, answer-key
fields, verdicts that cannot be recomputed from observations, and derived
verdicts that disagree with the independently retained oracle. The bundle-level
manifest becomes authoritative for containerized user outcomes only after all
required repetitions verify offline.
Individual repetitions never carry that authority, and production runs require
a clean tracked checkout so the source SHA remains meaningful.

The following boundaries are invariant:

- `native_performance_authority=false` for every Docker run;
- container startup is not fresh-install cost;
- container results are not final product scores or winner declarations;
- `realistic-default` remains the only active configuration slot;
- `expert-tuned` remains unavailable until the environment lock activates a
  separately reviewed tuning record;
- Kungfu remains a checksum-verified package consumer and never builds source
  inside the pilot.

Checked-in plans under `tests/fixtures/qualification-plans/` are marked
`test_only=true`. They prove that one runner resolves all four adapters but are
explicitly forbidden from issuing qualification receipts. They remain generic
`profile-smoke` checks and cannot be relabelled as declared comparator jobs. The
non-test `plans/postgres-phase-a-v1.json` plan covers J1/J2/J3 across all seven
Phase A tiers through the digest-locked PostgreSQL adapter.
