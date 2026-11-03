#!/usr/bin/env bash
# Warm wait: after sending a reply, the worker stays alive for a short
# idle window instead of exiting, so a follow-up message from the user
# is handled by the already-warm worker (context loaded) instead of
# paying a full cold start per message.
#
# Protocol: this script owns the marker file $STATE_DIR/ilink-warm.json
# ({since, activity, heartbeat, until}). While the marker's heartbeat
# is fresh, the hook script does NOT wake a second worker for newly
# landed batches — this waiter sees the pending file within a second
# and claims it for the worker. If the worker dies, the heartbeat goes
# stale and the hook resumes normal waking on its own (self-healing).
#
# Run it once after each reply (`ilink.py release` already raised the
# marker). It blocks, refreshing the heartbeat, and prints one of:
#   CLAIM_OK <lease> + one JSON line
#                 exactly `ilink.py claim` output: the batch is already
#                 claimed for you — handle it WITHOUT re-reading context
#                 files and WITHOUT claiming again
#   NEW_MESSAGES  a batch is waiting but could not be claimed here
#                 (ilink.py missing or failing) — run `ilink.py claim`
#   IDLE_TIMEOUT  nobody spoke for ILINK_WARM_IDLE seconds — exit
#   TOTAL_CAP     this warm session hit ILINK_WARM_TOTAL seconds
#                 since it started — exit (next message wakes fresh)
# CLAIM_BUSY (another worker holds a live claim and merges pending
# before it sends) and CLAIM_EMPTY (taken meanwhile) keep it waiting.
#
# Env: ILINK_HOOK_STATE_DIR (legacy alias ILINK_HOOK_STATE),
#      ILINK_DATA_DIR and ILINK_CLIENT (as for the watch script),
#      ILINK_WARM_IDLE (default 180), ILINK_WARM_TOTAL (default 1800),
#      ILINK_WARM_POLL (default 1). Like the watch script, it first reads
#      ilink-hook.env next to itself (or ILINK_HOOK_ENV), if present.
set -uo pipefail

HOOK_ENV=${ILINK_HOOK_ENV:-$(dirname "${BASH_SOURCE[0]}")/ilink-hook.env}
if [ -f "$HOOK_ENV" ]; then
  # shellcheck source=/dev/null
  source "$HOOK_ENV"
fi

STATE_DIR=${ILINK_HOOK_STATE_DIR:-${ILINK_HOOK_STATE:-$HOME/hooks/state}}
WARM=$STATE_DIR/ilink-warm.json
PENDING=$STATE_DIR/ilink-pending.json
IDLE=${ILINK_WARM_IDLE:-180}
TOTAL=${ILINK_WARM_TOTAL:-1800}
POLL=${ILINK_WARM_POLL:-1}
CLIENT=${ILINK_CLIENT:-${ILINK_DATA_DIR:-$HOME/wechat-ilink-data}/ilink.py}
# After CLAIM_BUSY, leave pending alone this long: its holder merges it
# before sending, and claiming every second would only log busy events.
BUSY_BACKOFF=10
busy_until=0

now_s=$(date +%s)
# Continue `since` from an existing marker so the total cap spans the
# whole warm session across successive waits; a marker left by a dead
# session has its cap in the past and ends this call immediately.
since=$now_s
if [ -f "$WARM" ]; then
  prev=$(jq -r '.since // empty' "$WARM" 2>/dev/null || true)
  [ -n "$prev" ] && since=$prev
fi
activity=$now_s
cap=$((since + TOTAL))
deadline=$((activity + IDLE))
[ "$deadline" -gt "$cap" ] && deadline=$cap

write_marker() {
  local tmp="$WARM.tmp.$$"
  jq -n --argjson s "$since" --argjson a "$activity" \
        --argjson h "$(date +%s)" --argjson u "$cap" \
        '{since: $s, activity: $a, heartbeat: $h, until: $u}' \
        > "$tmp" 2>/dev/null && mv "$tmp" "$WARM" 2>/dev/null || true
}
write_marker

while :; do
  n=$(date +%s)
  if [ -s "$PENDING" ] && [ "$n" -ge "$busy_until" ]; then
    cnt=$(jq '[.msgs[]?] | length' "$PENDING" 2>/dev/null || echo 0)
    if [ "${cnt:-0}" -gt 0 ] 2>/dev/null; then
      # Claim for the worker: it gets the batch in this output instead
      # of spending a model round on running claim itself. Keep the
      # marker either way: its heartbeat going stale is what re-arms
      # the hook for later batches if this worker dies.
      if [ -f "$CLIENT" ] && out=$(python3 "$CLIENT" claim 2>/dev/null); then
        case "$out" in
          CLAIM_OK*) printf '%s\n' "$out"; exit 0 ;;
          CLAIM_BUSY*) busy_until=$((n + BUSY_BACKOFF)) ;;
          CLAIM_EMPTY*) ;;
          *) echo NEW_MESSAGES; exit 0 ;;
        esac
      else
        echo NEW_MESSAGES
        exit 0
      fi
    fi
  fi
  if [ "$n" -ge "$deadline" ]; then
    rm -f "$WARM"
    if [ "$n" -ge "$cap" ]; then echo TOTAL_CAP; else echo IDLE_TIMEOUT; fi
    exit 0
  fi
  write_marker
  sleep "$POLL"
done
