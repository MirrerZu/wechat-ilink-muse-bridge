# The Muse hook variant: long polling inside Muse's hook mechanism

[中文](README.md) | [English](README.en.md)

`daemon.py` is a resident long-polling loop for ordinary environments. But
the Muse runtime already has agent scheduling built in — there is no need
to run another process just to call an LLM. This directory holds the other
wiring, the one actually used in production:

## Layout

```
WeChat user ──message──> iLink server
                              │  getupdates (long poll, server holds ≤35s)
                              ▼
                      ilink-watch.sh        ← Muse hook script, runs every 5s;
                              │               each run is one long poll
                     new msgs │  persist first, then wake
                              ▼
                ilink-pending.json          ← pending queue (atomic writes,
                              │               survives power loss)
                              │  worker: ilink.py claim → ilink-processing.json
                              ▼
                      Muse worker agent     ← pre-send queue re-check, merges
                                              bursts, sends exactly one reply;
                                              batches unprocessed after 10 min
                                              are returned to pending by the
                                              script and woken again
```

## Why the indirection

- **No lost messages**: the hook script writes messages to disk *before*
  waking the agent. If the agent crashes or times out, the messages are
  still in the queue and the next script run wakes it again.
- **One wake per batch**: a batch is woken once. While a warm worker
  or an open, unanswered claim exists, no wake goes out at all (that
  worker merges pending before sending); a batch nobody claimed is
  re-woken only after `ILINK_REWAKE_SECS` (default 180). The old
  "wake every round while pending exists" produced ~95 wakes for one
  batch and backed up wake delivery for over an hour.
- **Polling goes on behind a queued batch**: a round that wakes no one
  still polls, and new messages are appended to pending under the queue
  lock, so the worker gets them with its claim or its pre-send merge.
  The old "no poll while pending exists" left messages sent during
  composing on the server for a dozen seconds, often forcing one more
  `SEND_DEFERRED` rewrite.
- **No duplicate replies**: the worker claims with `ilink.py claim`
  (locked, shared with this script through `ilink-queue.lock`): a
  second worker gets `CLAIM_BUSY` and exits before reading any
  context. A cold worker claims with `claim --recent --with <today's
  diary>`: the batch, the last 24 hours of exchanges verbatim (from
  the quote cache) and the diary tail in one call, so its first model
  round can already write the reply. The pre-send re-check rides on
  the first bubble's `send --lease <lease>`, which moves new pending
  messages INTO the claim, so no one else answers them (with anything
  new it sends nothing and prints `SEND_DEFERRED`); `release <lease>`
  lets go once the reply is sent. Never `mv`:
  it overwrites another worker's claim.
- **Burst merging**: when a user fires off several messages, iLink splits
  them into multiple responses. The worker does not wait a fixed interval;
  it does a **pre-send re-check** — after composing a reply, right before
  sending, it looks at the queue once more. Anything new is merged in and
  the reply is re-composed against the newest context, so exactly one
  reply goes out per burst.
- **Instant "typing…"**: the moment a batch lands on disk, the script
  verifies the sender is the owner and lights the typing indicator right
  away, instead of waiting for the worker's cold start; the worker's own
  typing calls are just keep-alive refreshes.
- **Warm wait (no cold start mid-conversation)**: after replying, the
  worker runs `ilink-warm-wait.sh` (default 180s idle window, 30-minute
  session cap) instead of exiting. The waiter heartbeats a warm marker
  (`ilink-warm.json`); while it is fresh the script lands new batches
  without waking a second worker. The waiter checks the queue every
  second and claims a new batch for the worker, handing it over in
  its output, so the worker spends no model round on claiming and
  replies without re-reading context. The reply going out
  (`send --lease`) and `ilink.py release` already raise the marker, so
  a follow-up landing
  in the ten-odd seconds while the worker writes its diary, before the
  wait starts, goes to it too instead of waking a second, cold worker.
  A stale heartbeat (dead worker, 45 seconds) re-arms normal waking
  automatically.
- **Token expiry nags once**: when the script sees `TOKEN_EXPIRED` it
  creates a `rescan_notified` marker and wakes the agent to ask the user
  to re-scan; while the marker exists, it never nags again.
- **Closed-loop alerting** (an `alert-state.json` state machine): an
  expiry left unfixed is re-reported hourly; poll failures alert only
  after 10 continuous minutes (so network jitter never cries wolf),
  then hourly; when an alerted problem recovers, the agent wakes once
  to say it's back (un-alerted blips clear silently); when a worker's
  claim times out and is recycled, the pending wake carries a
  `stuck_alert` field; when the hook itself did not run for
  `ILINK_GAP_SECS` (default 600), its next run wakes `hook_gap` with
  the gap length — a cron watchdog that slept through the same outage
  only ever sees the refreshed heartbeat. Recovery (re-scan / network back) clears the
  expiry marker automatically — the old never-clear behavior would
  have suppressed the *next* expiry notice; fixed.
- **Delivery semantics: at-least-once.** Polling is two-phase:
  `ilink.py poll` only fetches — it neither advances the cursor nor
  records dedupe ids. The script durably writes the batch to the
  pending queue first, then calls `ilink.py commit`. A crash anywhere
  means the batch is simply re-fetched next round: a reply may
  duplicate, but a message is never silently lost. Supporting rules:
  a claim not renewed for 10 minutes is either dropped (its reply is
  already in the send ledger) or folded back IN FRONT of the pending
  queue — stray `ilink-processing-*.json` files included — never
  overwriting a newer batch; a valid empty queue is removed so polling
  resumes; and empty/invalid poll output or a non-zero business code
  is a poll failure (it enters the alert state machine instead of
  masquerading as "no new messages").
- **Event log**: wakes, claims, merges and releases append to
  `ilink-events.jsonl` (ids, counts, timings, never text), so one
  batch can be followed from arrival to release. An arrival is stamped
  when the poll returns and carries `poll_secs` (how long that long
  poll was held) and `lag_secs` (from the owner sending the oldest
  message to it being queued); `ilink.py stats` sums up today's pickup
  lag and claim wait.

Paths in the script are parameterized (`ILINK_DATA_DIR`, `ILINK_HOOK_STATE_DIR`,
`ILINK_CLIENT`; the legacy state-dir name `ILINK_HOOK_STATE` still works, and
the default matches `ilink.py health`). When the hook definition has no
environment field, put them (plus the venv's `PATH` when the bridge runs from
one) as `export` lines in `ilink-hook.env` next to the scripts: both scripts
source it first, and an upgrade never replaces it. `$HATCH_HOOK_RUNTIME` is provided by the Muse hook runtime;
outside Muse, use `daemon.py` in the repo root instead.
