#!/bin/bash
set -euo pipefail

/opt/aeron-native-kit/bin/aeron-native-harness version
archive_root=/tmp/aeron-native-kit-archive-smoke
marker=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
server_pid=
cleanup() {
  if [ -n "${server_pid}" ] && kill -0 "${server_pid}" 2>/dev/null; then
    kill -TERM "${server_pid}"
    wait "${server_pid}"
  fi
}
trap cleanup EXIT

/opt/aeron-native-kit/bin/aeron-native-harness server \
  --root "${archive_root}" \
  --file-sync-level 1 \
  --catalog-sync-level 1 \
  >"${archive_root}.server.log" 2>&1 &
server_pid=$!
for _ in $(seq 1 30); do
  if /opt/aeron-native-kit/bin/aeron-native-harness health --root "${archive_root}" \
    >"${archive_root}.health.json" 2>/dev/null; then
    break
  fi
  sleep 1
done
/opt/aeron-native-kit/bin/aeron-native-harness health --root "${archive_root}"
record_output=$(/opt/aeron-native-kit/bin/aeron-native-harness record \
  --root "${archive_root}" --count 5 --payload 128 --receipt durable_sync --marker "${marker}")
recording_id=$(printf '%s\n' "${record_output}" | sed -n 's/.*"recording_id":\([0-9][0-9]*\).*/\1/p')
recording_length=$(printf '%s\n' "${record_output}" | sed -n 's/.*"final_position":\([0-9][0-9]*\).*/\1/p')
test -n "${recording_id}"
test -n "${recording_length}"
/opt/aeron-native-kit/bin/aeron-native-harness replay \
  --root "${archive_root}" --count 5 --recording-id "${recording_id}" \
  --length "${recording_length}" --marker "${marker}"
cleanup
server_pid=

/opt/aeron-native-kit/bin/aeron-native-harness ipc \
  --root /tmp/aeron-native-kit-smoke \
  --warmup 100 \
  --messages 500 \
  --payload 64 \
  --rate 10000 \
  --histogram /tmp/aeron-native-kit-smoke.hlog
test -s /tmp/aeron-native-kit-smoke.hlog
