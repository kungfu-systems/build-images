# Comparator Formal Runner

This image is an immutable distribution envelope for the formal comparator
runner, frozen plan template, schemas, and offline verifier. The measured
runner executes on the Linux host after extraction. It is never run with a
Docker socket mount, host networking, privilege, or host tuning.

Extract the exact published image:

```bash
image='ghcr.io/kungfu-systems/build-images/comparator-formal-runner@sha256:REPLACE'
container="$(docker create "${image}")"
mkdir -p ./formal-performance-kit
docker cp "${container}:/opt/formal-performance/." ./formal-performance-kit/
docker rm "${container}"
./formal-performance-kit/bin/formal-performance validate-plan \
  --plan ./formal-performance-kit/contracts/formal-performance-v1.json
```

The runner keeps `winner_authority=false`. Machine aggregates are descriptive
only. Active-operator timing and blinded M3/M4 reviews remain external,
digest-bound attachments and are never self-graded.

The repository-owned preparation command pulls all exact inputs before timing,
extracts this kit, builds the frozen Kungfu source and external matched driver,
and emits the environment consumed by `run`:

```bash
python3 pilots/comparator/scripts/prepare_formal_performance.py \
  --runner-image "${image}" \
  --builder-image "ghcr.io/kungfu-systems/build-images/kungfu-native-linux-x64@sha256:REPLACE" \
  --kungfu-source /absolute/path/to/clean-kungfu-d6fb387 \
  --kungfu-package /absolute/path/to/kungfu-episodes-cli-linux-x64.tar.gz \
  --kungfu-version VERSION \
  --kungfu-evidence-url https://github.com/kungfu-systems/kungfu/actions/runs/RUN \
  --cache-home /absolute/path/to/formal-performance-cache \
  --scratch-root /data/formal-performance-scratch \
  --staging /absolute/path/to/empty-staging \
  --execute

/absolute/path/to/empty-staging/kit/bin/formal-performance run \
  --plan /absolute/path/to/empty-staging/kit/contracts/formal-performance-v1.json \
  --output /absolute/path/to/formal-performance-bundle \
  --runner-image "${image}" \
  --preparation /absolute/path/to/empty-staging/preparation.json
```

The production `run` command has no provider override. Every measured Docker
startup uses `--pull never` with an internal or disabled network.
Matched samples retain separate `crash_replay_ns` and
`whole_root_restore_ns` counters. Only the frozen `recovery` workload performs
the whole-root materialization; `recovery_ns` is always their exact sum and is
the input to the cross-product recovery amplification.
The 60-second matched soak is capped symmetrically at 10,000 messages per
second so both drivers retain a bounded, fully replayable correctness set under
the common 2 GiB subject limit. Latency and throughput workloads remain
unthrottled.
