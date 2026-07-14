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
if [ "$status" != "ready" ]; then
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

run_id=${GITHUB_RUN_ID:-manual-$(date -u +%Y%m%dT%H%M%SZ)-$$}
project=${COMPARATOR_PROJECT_NAME:-kf-comparator-${profile}-${run_id}}
project=$(printf '%s' "$project" | tr -c 'a-zA-Z0-9_-' '-')
report_dir="$ARTIFACT_ROOT/$project"

compose() {
  COMPARATOR_PROJECT_NAME="$project" docker compose -f "$COMPOSE_FILE" --project-name "$project" "$@"
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
    compose kill -s SIGKILL postgres
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
    compose kill -s SIGKILL clickhouse
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
    compose exec -T aeron java \
      --add-opens java.base/jdk.internal.misc=ALL-UNNAMED \
      --add-opens java.base/java.util.zip=ALL-UNNAMED \
      '-Daeron.archive.control.channel=aeron:udp?endpoint=localhost:8010' \
      '-Daeron.archive.replication.channel=aeron:udp?endpoint=localhost:0' \
      '-Daeron.archive.control.response.channel=aeron:udp?endpoint=localhost:0' \
      -Daeron.sample.messages=5 \
      -cp /opt/aeron/aeron-all.jar \
      io.aeron.samples.archive.RecordedBasicPublisher \
      | tee "$report_dir/aeron-recording.txt"
    grep -Fq "yay!" "$report_dir/aeron-recording.txt"
    if grep -Fq "Offer failed" "$report_dir/aeron-recording.txt"; then
      echo "Aeron publisher reported an unsuccessful offer" >&2
      exit 1
    fi
    compose exec -T aeron sh -c "find /var/lib/aeron/archive -type f -print | sort" \
      | tee "$report_dir/aeron-archive-files.txt"
    compose kill -s SIGKILL aeron
    compose cp aeron:/var/lib/aeron/archive "$report_dir/aeron-archive"
    sleep 11
    compose --profile aeron up -d --wait aeron
    compose exec -T aeron sh -c "test -n \"\$(find /var/lib/aeron/archive -type f -print -quit)\""
    compose exec -T aeron mkdir -p /var/lib/aeron/restore
    compose cp "$report_dir/aeron-archive/." aeron:/var/lib/aeron/restore/
    compose exec -T aeron sh -c "test -n \"\$(find /var/lib/aeron/restore -type f -print -quit)\""
    ;;
esac

compose ps --format json >"$report_dir/compose-ps.json"
echo "UNSCORED comparator smoke passed: $profile"
