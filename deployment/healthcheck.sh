#!/bin/sh
# Exit 0 when the API answers and the host engine holds its lock.
set -eu
URL="${1:-http://127.0.0.1:8787/api/health}"
out=$(curl -fsS --max-time 5 "$URL") || { echo "API unreachable"; exit 2; }
echo "$out"
echo "$out" | grep -q '"engine_running":true' || { echo "engine not running"; exit 1; }
