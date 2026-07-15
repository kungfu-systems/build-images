#!/bin/bash
set -euo pipefail

/opt/aeron-native-kit/bin/aeron-native-harness version
/opt/aeron-native-kit/bin/aeron-native-harness ipc \
  --root /tmp/aeron-native-kit-smoke \
  --warmup 100 \
  --messages 500 \
  --payload 64 \
  --rate 10000 \
  --histogram /tmp/aeron-native-kit-smoke.hlog
test -s /tmp/aeron-native-kit-smoke.hlog
