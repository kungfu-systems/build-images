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
| Kungfu | source SHA + builder/runtime image digests + Rust bootstrap pins | Ready | Episode write/query, bundle export, forced restart, fsck, isolated restore |

The Kungfu pilot is built from the public source snapshot named in
`environment.lock.json`. It is not presented as a released product artifact.
The hosted job performs the source build, so its setup time and failure modes
remain visible instead of being hidden behind a developer-machine binary.

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

## Kungfu source-build boundary

The source-build profile is appropriate for disposable functional and recovery
qualification while Kungfu remains pre-release. It deliberately separates two
questions:

1. Can a fresh, neutral environment reproduce and exercise the current public
   source at one exact commit?
2. What will a real user pay to install a released Kungfu distribution?

This kit answers only the first question. Downstream comparisons must retain
the source checkout, dependency setup, build duration, network transfer and
failure evidence as Kungfu setup cost. They must not report the prebuilt Docker
image startup time as time-to-trusted-answer.

The lock records the exact source commit, builder and runtime image digests,
Rust bootstrap checksum/toolchain, and the Shifu build entrypoint, including
fresh-host Conan profile detection. It also selects the source contract's
qualified Clang 18 Linux secondary compiler; the builder digest fixes the
actual compiler environment. The source tree then supplies its own Node, pnpm,
Python, Cargo, Conan and package locks.
The smoke writes and seals an Episode, proves it through the query surface,
exports it, kills the container, checks the retained journal after restart,
and imports the bundle into a separate workspace.

When a public Kungfu product artifact becomes available, it may be added as a
separate installation mode with its own checksum and release evidence. That
future user-install path does not block this explicitly unscored source pilot,
and it must not silently replace the retained source-build cost record.
