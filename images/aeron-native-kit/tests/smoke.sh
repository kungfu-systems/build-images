#!/bin/bash
set -euo pipefail

/opt/aeron-native-kit/bin/aeron-native-harness version
archive_root=/tmp/aeron-native-kit-archive-smoke
marker=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
server_pid=
cleanup() {
  if [ -n "${server_pid}" ] && kill -0 "${server_pid}" 2>/dev/null; then
    kill -TERM "${server_pid}"
    server_status=0
    wait "${server_pid}" || server_status=$?
    if [ "${server_status}" -ne 0 ] && [ "${server_status}" -ne 143 ]; then
      return "${server_status}"
    fi
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
  if ! kill -0 "${server_pid}" 2>/dev/null; then
    cat "${archive_root}.server.log" >&2
    exit 1
  fi
  if timeout 5 /opt/aeron-native-kit/bin/aeron-native-harness health --root "${archive_root}" \
    >"${archive_root}.health.json" 2>/dev/null; then
    break
  fi
  sleep 1
done
timeout 5 /opt/aeron-native-kit/bin/aeron-native-harness health --root "${archive_root}"
record_output=$(/opt/aeron-native-kit/bin/aeron-native-harness record \
  --root "${archive_root}" --count 5 --payload 128 --receipt durable_sync --marker "${marker}")
recording_id=$(printf '%s\n' "${record_output}" | sed -n 's/.*"recording_id":\([0-9][0-9]*\).*/\1/p')
recording_length=$(printf '%s\n' "${record_output}" | sed -n 's/.*"final_position":\([0-9][0-9]*\).*/\1/p')
receipt_duration=$(printf '%s\n' "${record_output}" | sed -n 's/.*"receipt_duration_ns":\([0-9][0-9]*\).*/\1/p')
completion_duration=$(printf '%s\n' "${record_output}" | sed -n 's/.*"completion_duration_ns":\([0-9][0-9]*\).*/\1/p')
test -n "${recording_id}"
test -n "${recording_length}"
test -n "${receipt_duration}"
test -n "${completion_duration}"
test "${completion_duration}" -ge "${receipt_duration}"
wrong_marker=ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
if /opt/aeron-native-kit/bin/aeron-native-harness replay \
  --root "${archive_root}" --count 5 --recording-id "${recording_id}" \
  --length "${recording_length}" --marker "${wrong_marker}" >/dev/null 2>&1; then
  echo "replay accepted a mismatched marker" >&2
  exit 1
fi
/opt/aeron-native-kit/bin/aeron-native-harness replay \
  --root "${archive_root}" --count 5 --recording-id "${recording_id}" \
  --length "${recording_length}" --marker "${marker}"
cleanup
server_pid=
if timeout 5 /opt/aeron-native-kit/bin/aeron-native-harness health \
  --root "${archive_root}" >/dev/null 2>&1; then
  echo "health accepted stale Driver/Archive files" >&2
  exit 1
fi

ipc_output=$(/opt/aeron-native-kit/bin/aeron-native-harness ipc \
  --root /tmp/aeron-native-kit-smoke \
  --warmup 100 \
  --messages 500 \
  --payload 64 \
  --rate 10000 \
  --seed 17001 \
  --poll-batch 1 \
  --histogram /tmp/aeron-native-kit-smoke.hlog)
printf '%s\n' "${ipc_output}"
histogram_start=$(printf '%s\n' "${ipc_output}" | sed -n 's/.*"histogram_start_time_ms":\([0-9][0-9]*\).*/\1/p')
histogram_end=$(printf '%s\n' "${ipc_output}" | sed -n 's/.*"histogram_end_time_ms":\([0-9][0-9]*\).*/\1/p')
observed=$(printf '%s\n' "${ipc_output}" | sed -n 's/.*"observed":\([0-9][0-9]*\).*/\1/p')
test -n "${histogram_start}"
test -n "${histogram_end}"
test "${observed}" -eq 500
printf '%s\n' "${ipc_output}" | grep -q '"loss":0'
printf '%s\n' "${ipc_output}" | grep -q '"duplicates":0'
printf '%s\n' "${ipc_output}" | grep -q '"reordered":0'
printf '%s\n' "${ipc_output}" | grep -q '"offer_failures":0'
printf '%s\n' "${ipc_output}" | grep -q '"seed":17001'
printf '%s\n' "${ipc_output}" | grep -q '"poll_batch":1'
test "${histogram_end}" -ge "${histogram_start}"
test -s /tmp/aeron-native-kit-smoke.hlog
