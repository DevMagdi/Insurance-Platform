#!/usr/bin/env bash
# Run a CI command; on failure surface the tail of its output as a GitHub
# annotation (readable from the checks API even when the log archive host is
# unreachable), then propagate the exit code.
#   scripts/ci/annotate.sh "<title>" <command> [args...]
set -uo pipefail
title=$1
shift
log=$(mktemp)
"$@" 2>&1 | tee "$log"
code=${PIPESTATUS[0]}
if [ "$code" -ne 0 ]; then
    tail -c 3500 "$log" | sed 's/\x1b\[[0-9;]*m//g' | tr -d '\r' | sed 's/%/%25/g' | awk 'BEGIN { ORS = "%0A" } { print }' |
        sed "s/^/::error title=${title//:/-}::/"
    echo
fi
rm -f "$log"
exit "$code"
