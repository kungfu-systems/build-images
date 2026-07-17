#!/bin/bash

wait_for_sigkill() {
  service=$1
  state_dir=$2
  SIGKILL_CONTAINER_ID=$(compose ps -q "$service")
  if [ -z "$SIGKILL_CONTAINER_ID" ]; then
    echo "Cannot crash $service: running container id is missing" >&2
    return 1
  fi

  compose kill -s SIGKILL "$service"
  attempts=0
  while [ "$attempts" -lt 100 ]; do
    state=$(docker inspect --format '{{.State.Running}} {{.State.ExitCode}}' "$SIGKILL_CONTAINER_ID" 2>/dev/null || true)
    case "$state" in
      "false 137")
        printf 'container_id=%s\nstate=%s\n' "$SIGKILL_CONTAINER_ID" "$state" \
          >"$state_dir/${service}-sigkill-state.txt"
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

record_restart_state() {
  service=$1
  previous_container_id=$2
  state_dir=$3
  container_id=$(compose ps -q "$service")
  if [ -z "$container_id" ]; then
    echo "Cannot verify $service restart: running container id is missing" >&2
    return 1
  fi

  state=$(docker inspect --format '{{.State.Running}} {{.State.ExitCode}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id" 2>/dev/null || true)
  case "$state" in
    "true 0 healthy"|"true 0 none")
      printf 'previous_container_id=%s\ncontainer_id=%s\nstate=%s\n' \
        "$previous_container_id" "$container_id" "$state" \
        >"$state_dir/${service}-restart-state.txt"
      ;;
    *)
      echo "Restart state for $service is not healthy/running: $state" >&2
      return 1
      ;;
  esac
}
