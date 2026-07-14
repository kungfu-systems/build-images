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
`pilot_unscored: true` and `performance_authority: false`. Docker results cannot
replace the designated native-host performance program, installation-cost
study, or the final user-perspective comparison protocol.

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
