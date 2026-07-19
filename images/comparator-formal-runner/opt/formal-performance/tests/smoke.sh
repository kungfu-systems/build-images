#!/bin/bash
set -euo pipefail

root=/opt/formal-performance
runner="${root}/bin/formal-performance"
plan="${root}/contracts/formal-performance-v1.json"

"${runner}" validate-plan --plan "${plan}"
test "$("${runner}" print-schedule --plan "${plan}" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')" = "666"
"${runner}" self-test
test -x "${root}/bin/formal-performance-provider"
echo "authority=measurement-only,winner=false"
