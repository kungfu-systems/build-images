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
| Kungfu | Formal product artifact + SHA-256 + evidence required | Blocked | deliberately unavailable until a formal product artifact is published |

The public `shifu-v4.0.0-alpha.0` launcher assets and locally produced ADR-0049
qualification packages are not accepted as Kungfu product artifacts. The
Kungfu profile fails closed instead of silently comparing a substitute.

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

## Configuration slots

`realistic-default` is the only active slot. `expert-tuned` is reserved until a
separately reviewed product-specific tuning record exists. A tuned run must not
quietly replace the default user path.

## Promoting the Kungfu profile

Promotion requires all of the following in one reviewed change:

1. a public formal Kungfu product version;
2. an immutable artifact URL and exact SHA-256;
3. release/qualification evidence identifying the same artifact;
4. an installer and product-specific functional/recovery smoke contract;
5. inclusion in the hosted smoke matrix only after the previous checks pass.
