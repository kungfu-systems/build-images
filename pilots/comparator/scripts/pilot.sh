#!/bin/bash

set -euo pipefail

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
PILOT_DIR=$(CDPATH='' cd -- "$SCRIPT_DIR/.." && pwd)
COMPOSE_FILE="$PILOT_DIR/compose.yaml"
ARTIFACT_ROOT="$PILOT_DIR/.artifacts"

usage() {
  cat <<'EOF'
Usage:
  pilot.sh plan <aeron|clickhouse|postgres|kungfu>
  pilot.sh smoke <aeron|clickhouse|postgres|kungfu> --execute

plan is read-only. smoke requires --execute and only operates on a unique,
disposable Compose project owned by this invocation.
EOF
}

if [ "$#" -lt 2 ]; then
  usage >&2
  exit 64
fi

action=$1
profile=$2
execute=false
if [ "${3:-}" = "--execute" ]; then
  execute=true
elif [ "$#" -gt 2 ]; then
  usage >&2
  exit 64
fi

case "$profile" in
  aeron|clickhouse|postgres|kungfu) ;;
  *) echo "Unknown profile: $profile" >&2; exit 64 ;;
esac

python3 "$SCRIPT_DIR/comparator_pilot.py" validate

status=$(python3 - "$PILOT_DIR/environment.lock.json" "$profile" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle)["profiles"][sys.argv[2]]["status"])
PY
)
if [ "$status" != "ready" ] && [ "$profile:$status" != "kungfu:package-input-required" ]; then
  reason=$(python3 - "$PILOT_DIR/environment.lock.json" "$profile" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    profile = json.load(handle)["profiles"][sys.argv[2]]
print(profile.get("blocked_reason", profile["status"]))
PY
)
  echo "Profile $profile is fail-closed: $reason" >&2
  exit 3
fi

run_id=${GITHUB_RUN_ID:-manual-$(date -u +%Y%m%dt%H%M%Sz)-$$}
project=${COMPARATOR_PROJECT_NAME:-kf-comparator-${profile}-${run_id}}
project=$(printf '%s' "$project" | tr -c 'a-zA-Z0-9_-' '-')
report_dir="$ARTIFACT_ROOT/$project"

compose() {
  COMPARATOR_PROJECT_NAME="$project" docker compose -f "$COMPOSE_FILE" --project-name "$project" "$@"
}

wait_for_sigkill() {
  service=$1
  container_id=$(compose ps -q "$service")
  if [ -z "$container_id" ]; then
    echo "Cannot crash $service: running container id is missing" >&2
    return 1
  fi

  compose kill -s SIGKILL "$service"
  attempts=0
  while [ "$attempts" -lt 100 ]; do
    state=$(docker inspect --format '{{.State.Running}} {{.State.ExitCode}}' "$container_id" 2>/dev/null || true)
    case "$state" in
      "false 137")
        printf 'container_id=%s\nstate=%s\n' "$container_id" "$state" \
          >"$report_dir/${service}-sigkill-state.txt"
        return 0
        ;;
      "false "*)
        echo "Crash state for $service is not SIGKILL: $state" >&2
        return 1
        ;;
    esac
    attempts=$((attempts + 1))
    sleep 0.2
  done

  echo "Timed out waiting for $service SIGKILL state" >&2
  return 1
}

print_plan() {
  echo "UNSCORED Docker pilot plan"
  echo "profile=$profile"
  echo "project=$project"
  echo "report_dir=$report_dir"
  echo "docker compose -f $COMPOSE_FILE --project-name $project --profile $profile up -d --wait runner $profile"
  echo "<profile-specific normal, SIGKILL/restart, recovery, and export/restore probes>"
  echo "docker compose -f $COMPOSE_FILE --project-name $project --profile $profile down --volumes --remove-orphans"
  echo "No host networking, privileged mode, Docker socket mount, or global cleanup is used."
  if [ "$profile" = "kungfu" ]; then
    echo "Kungfu requires a prebuilt kungfu-episodes-cli-linux-x64.tar.gz plus version, source SHA, package SHA-256, and evidence URL inputs."
    echo "Kungfu source is not compiled by this Docker pilot."
  fi
}

if [ "$action" = "plan" ]; then
  print_plan
  exit 0
fi
if [ "$action" != "smoke" ] || [ "$execute" != true ]; then
  usage >&2
  exit 64
fi

command -v docker >/dev/null 2>&1 || { echo "docker is required" >&2; exit 69; }
docker compose version >/dev/null
mkdir -p "$report_dir"
python3 "$SCRIPT_DIR/comparator_pilot.py" emit-manifest \
  --profile "$profile" \
  --project "$project" \
  --output "$report_dir/environment-manifest.json"

cleanup() {
  compose logs --no-color >"$report_dir/compose.log" 2>&1 || true
  compose --profile "$profile" down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

compose --profile "$profile" config --quiet
compose --profile "$profile" up -d --wait runner "$profile"

case "$profile" in
  postgres)
    compose exec -T postgres psql -U pilot -d pilot -v ON_ERROR_STOP=1 -c \
      "CREATE TABLE IF NOT EXISTS pilot_events (id integer PRIMARY KEY, payload text NOT NULL); INSERT INTO pilot_events VALUES (1, 'seed') ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload;"
    compose exec -T postgres pg_dump -U pilot -d pilot --table=pilot_events >"$report_dir/postgres-pilot-events.sql"
    wait_for_sigkill postgres
    compose --profile postgres up -d --wait postgres
    compose exec -T postgres psql -U pilot -d pilot -Atc "SELECT count(*) FROM pilot_events" | tee "$report_dir/postgres-restart-count.txt"
    compose exec -T postgres psql -U pilot -d pilot -v ON_ERROR_STOP=1 -c "DROP TABLE pilot_events"
    compose exec -T postgres psql -U pilot -d pilot -v ON_ERROR_STOP=1 <"$report_dir/postgres-pilot-events.sql"
    compose exec -T postgres psql -U pilot -d pilot -Atc "SELECT payload FROM pilot_events WHERE id = 1" | tee "$report_dir/postgres-restore-value.txt"
    ;;
  clickhouse)
    compose exec -T clickhouse clickhouse-client --user pilot --password pilot-local-only --multiquery --query \
      "CREATE TABLE IF NOT EXISTS pilot.pilot_events (id UInt32, payload String) ENGINE = ReplacingMergeTree ORDER BY id; INSERT INTO pilot.pilot_events VALUES (1, 'seed');"
    compose exec -T clickhouse clickhouse-client --user pilot --password pilot-local-only --query \
      "SELECT * FROM pilot.pilot_events FORMAT Native" >"$report_dir/clickhouse-pilot-events.native"
    wait_for_sigkill clickhouse
    compose --profile clickhouse up -d --wait clickhouse
    compose exec -T clickhouse clickhouse-client --user pilot --password pilot-local-only --query \
      "SELECT count() FROM pilot.pilot_events" | tee "$report_dir/clickhouse-restart-count.txt"
    compose exec -T clickhouse clickhouse-client --user pilot --password pilot-local-only --query \
      "TRUNCATE TABLE pilot.pilot_events"
    compose exec -T clickhouse clickhouse-client --user pilot --password pilot-local-only --query \
      "INSERT INTO pilot.pilot_events FORMAT Native" <"$report_dir/clickhouse-pilot-events.native"
    compose exec -T clickhouse clickhouse-client --user pilot --password pilot-local-only --query \
      "SELECT payload FROM pilot.pilot_events WHERE id = 1" | tee "$report_dir/clickhouse-restore-value.txt"
    ;;
  aeron)
    harness=/opt/aeron-native-kit/bin/aeron-native-harness
    marker=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
    compose exec -T aeron "$harness" record \
      --root /var/lib/aeron \
      --count 5 \
      --payload 128 \
      --receipt durable_sync \
      --marker "$marker" \
      | tee "$report_dir/aeron-recording.txt"
    recording_values=$(python3 - "$report_dir/aeron-recording.txt" "$marker" <<'PY'
import json
import sys

receipt = json.loads(open(sys.argv[1], encoding="utf-8").read())
if (
    receipt.get("schema") != "aeron-record-receipt/v2"
    or receipt.get("observed") != 5
    or receipt.get("duplicates") != 0
    or receipt.get("reordered") != 0
    or receipt.get("marker_mismatches") != 0
    or receipt.get("marker") != sys.argv[2]
):
    raise SystemExit("Aeron record receipt failed its smoke oracle")
print(receipt["recording_id"], receipt["final_position"])
PY
)
    recording_id=${recording_values%% *}
    recording_length=${recording_values#* }
    compose exec -T aeron sh -c "find /var/lib/aeron/archive -type f -print | sort" \
      | tee "$report_dir/aeron-archive-files.txt"
    wait_for_sigkill aeron
    compose cp aeron:/var/lib/aeron/archive "$report_dir/aeron-archive"
    sleep 11
    compose --profile aeron up -d --wait aeron
    compose exec -T aeron sh -c "test -n \"\$(find /var/lib/aeron/archive -type f -print -quit)\""
    compose exec -T aeron "$harness" replay \
      --root /var/lib/aeron \
      --count 5 \
      --recording-id "$recording_id" \
      --length "$recording_length" \
      --marker "$marker" \
      | tee "$report_dir/aeron-replay.txt"
    python3 - "$report_dir/aeron-replay.txt" "$marker" <<'PY'
import json
import sys

receipt = json.loads(open(sys.argv[1], encoding="utf-8").read())
if (
    receipt.get("schema") != "aeron-replay-receipt/v2"
    or receipt.get("observed") != 5
    or receipt.get("duplicates") != 0
    or receipt.get("reordered") != 0
    or receipt.get("marker_mismatches") != 0
    or receipt.get("marker") != sys.argv[2]
):
    raise SystemExit("Aeron replay receipt failed its smoke oracle")
PY
    compose exec -T aeron mkdir -p /var/lib/aeron/restore
    compose cp "$report_dir/aeron-archive/." aeron:/var/lib/aeron/restore/
    compose exec -T aeron sh -c "test -n \"\$(find /var/lib/aeron/restore -type f -print -quit)\""
    ;;
  kungfu)
    episode_id=424242
    primary_home=/var/lib/kungfu/primary
    bundle=/var/lib/kungfu/episode-${episode_id}.json
    restore_workspace=/var/lib/kungfu/restore-workspace
    restore_home=${restore_workspace}/.kungfu
    compose exec -T kungfu kungfu -H "$primary_home" storage episode begin \
      --episode-id "$episode_id" \
      --title "comparator pilot" \
      --actor "unscored-docker" \
      --source "build-images" \
      --json | tee "$report_dir/kungfu-episode-begin.json"
    compose exec -T kungfu kungfu -H "$primary_home" storage episode end \
      --episode-id "$episode_id" \
      --reason "pilot-complete" \
      --json | tee "$report_dir/kungfu-episode-end.json"
    compose exec -T kungfu kungfu -H "$primary_home" storage episode rebuild-projection \
      --json | tee "$report_dir/kungfu-projection-before-restart.json"
    compose exec -T kungfu kungfu -H "$primary_home" query prove \
      --episode-id "$episode_id" \
      --json | tee "$report_dir/kungfu-query-proof-before-restart.json"
    compose exec -T kungfu kungfu -H "$primary_home" storage export \
      --scope episode \
      --episode-id "$episode_id" \
      --format bundle-json \
      --out "$bundle" \
      --json | tee "$report_dir/kungfu-export.json"
    wait_for_sigkill kungfu
    compose --profile kungfu up -d --wait kungfu
    compose exec -T kungfu kungfu -H "$primary_home" storage episode inspect \
      --episode-id "$episode_id" \
      --json | tee "$report_dir/kungfu-inspect-after-restart.json"
    compose exec -T kungfu kungfu -H "$primary_home" storage fsck \
      --scope episode \
      --episode-id "$episode_id" \
      --json | tee "$report_dir/kungfu-fsck-after-restart.json"
    compose exec -T kungfu kungfu -H "$primary_home" storage import \
      --from "$bundle" \
      --execute \
      --workspace "$restore_workspace" \
      --json | tee "$report_dir/kungfu-import.json"
    compose exec -T kungfu kungfu -H "$restore_home" storage episode inspect \
      --episode-id "$episode_id" \
      --json | tee "$report_dir/kungfu-restored-episode.json"
    grep -Fq "\"episode_id\": $episode_id" "$report_dir/kungfu-inspect-after-restart.json"
    grep -Fq '"ok": true' "$report_dir/kungfu-fsck-after-restart.json"
    grep -Fq "\"episode_id\": $episode_id" "$report_dir/kungfu-restored-episode.json"
    ;;
esac

compose ps --format json >"$report_dir/compose-ps.json"
compose config --format json >"$report_dir/compose-config.json"
docker version --format '{{json .}}' >"$report_dir/docker-version.json"
docker compose version --short >"$report_dir/docker-compose-version.txt"
uname -a >"$report_dir/host-kernel.txt"
: >"$report_dir/docker-stats.jsonl"
: >"$report_dir/container-inspect.jsonl"
compose ps -q | while IFS= read -r container_id; do
  [ -n "$container_id" ] || continue
  docker stats --no-stream --format '{{json .}}' "$container_id" >>"$report_dir/docker-stats.jsonl"
  docker inspect --format '{{json .}}' "$container_id" >>"$report_dir/container-inspect.jsonl"
done
echo "UNSCORED comparator smoke passed: $profile"
