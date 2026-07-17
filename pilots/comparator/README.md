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
and imports the bundle into a separate workspace. Each deliberate crash retains
the stopped `false 137` state and the subsequent running/healthy container
identity in profile-specific `*-sigkill-state.txt` and `*-restart-state.txt`
artifacts.

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
only reproducible execution and bundle integrity. Containerized production
plans exist for PostgreSQL, ClickHouse, and Aeron. Each plan binds its selected
adapter,
digest-loaded semantics module, execution fixture, verifier-only oracle,
Compose file, environment lock, exact subject image, and exact runner image.

The ClickHouse subject is the build-images-owned `clickhouse-server` GHCR
mirror. Its Dockerfile pins the reviewed upstream `26.3.10.60-lts` digest, and
the production plan consumes an accepted GHCR digest from an earlier reviewed
alpha. Formal hosts therefore never need an unbounded direct Docker Hub pull.

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

Before service startup, `run` derives and validates the complete set of unique
Compose project names for every repetition and step. Names are normalized to
portable lowercase Compose syntax, capped at 63 characters, and reused without
change by execution and project-scoped cleanup. One `compose config --quiet`
preflight against the locked file fails the whole run before any service starts,
instead of repeating a deterministic environment or project-name error.

Before repetition 1, a separate unscored preparation phase resolves every
service in the selected Compose profile and pulls each image by its exact locked
digest and platform. Transient registry failures receive at most three attempts
with fixed 2-second and 5-second delays. The bundle retains each command,
attempt, timing, stdout/stderr digest, local image ID, and repository digest.
Exhausted or non-transient failures stop the run before any formal step. Every
counted `compose up` uses `--pull never`, so registry work and image identity
changes cannot enter a measured step.

Each repetition retains step timing, process status, cleanup status, oracle
results, service logs, Docker/Compose and kernel facts, Compose resource
limits, container inspect data, CPU/memory observations, and SHA-256 records
for every raw artifact. Every step also retains a project-label-scoped cleanup
record proving that no project containers, volumes, or networks remain; the
offline verifier rejects missing, altered, timed-out, or non-empty cleanup
evidence. Every tier event is followed by the same job-specific
facts query; the exact query result, tier postcondition, and derived verdict are
retained separately. The execution fixture contains no expected outcome. The
self-contained bundle also retains the exact Compose file, environment lock,
pilot adapter, qualification runner, workload adapter registry, adapter
artifact, semantic evaluator, execution fixture, verifier-only oracle, and
their digests. The full registry is digest-bound, while offline validation is
scoped to the selected adapter closure; unrelated adapter files are not hidden
dependencies of a self-contained bundle. It also binds the verified
image-preparation manifest and its raw
logs; the offline verifier rejects missing, altered, or extra preparation
artifacts. Every run binds the scenario, declared job, tier, action,
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
non-test `plans/postgres-phase-a-v1.json`,
`plans/clickhouse-phase-a-v1.json`, and `plans/aeron-phase-a-v1.json` plans
each cover J1/J2/J3 across all seven Phase A tiers through their digest-locked
adapters.

## Aeron native qualification

`aeron_native_qualification.py` is the separate native Linux x86-64 authority
for Aeron performance, receipts, recovery, and the bounded soak. It consumes
the exact kit declared by `plans/aeron-native-phase-a-v1.json`; Docker is used
only to extract that digest-locked kit before measured execution, and no
container remains on the measured path.

An authoritative run must start from an exact public build-images release tag
and the matching public `buildchain.release.json` and `check-report.json`
assets. Before preparing the kit, the runner fails closed unless all of the
following agree: the clean checkout HEAD, the exact tag at HEAD, the official
GitHub origin tag, the release passport material/source SHAs, the release
check report trust verdict, and the SHA-256 digests published for both assets
by the GitHub release API.

```bash
python3 pilots/comparator/scripts/aeron_native_qualification.py \
  validate-plan --plan pilots/comparator/plans/aeron-native-phase-a-v1.json

python3 pilots/comparator/scripts/aeron_native_qualification.py run \
  --plan pilots/comparator/plans/aeron-native-phase-a-v1.json \
  --release-passport /path/to/buildchain.release.json \
  --release-check-report /path/to/check-report.json \
  --execute
```

The completed bundle retains the exact runner, release inputs, public-release
receipt, contracts, plan, host facts, kit preparation evidence, raw results,
and a digest inventory. Offline verification must use the bundled runner so
the verifier itself is part of the retained evidence closure:

```bash
python3 <bundle>/authority/scripts/aeron_native_qualification.py \
  verify-bundle --bundle <bundle>
```

The verifier requires the executing runner to match the bundled runner digest
and revalidates all retained release, contract, plan, result, calibration, and
artifact bindings without network access. A locally invented tag or a
self-consistent locally generated passport is not release authority.

The `Comparator Qualification Contracts` workflow exposes a separately gated
`run_lifecycle` dispatch input plus PostgreSQL and ClickHouse pull-request
labels. The dispatch runs both complete frozen 3x21 plans on a GitHub-hosted
Docker worker, verifies each resulting bundle offline, injects a separate
PostgreSQL workload failure, proves that its scoped containers, volumes, and
networks are also gone, and retains all evidence as a 30-day workflow artifact.
Ordinary pull requests do not start this long-running lifecycle job.
