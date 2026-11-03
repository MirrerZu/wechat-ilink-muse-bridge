# AGENTS.md — instructions for the agent deploying this bridge

You are an AI agent (typically Muse) whose user asked you to connect WeChat
to yourself using this repository. Follow these steps. Only one step needs
the human: scanning a QR code.

## 0. Prerequisites

- Python 3.10+ and `pip install -r requirements.txt` (the only hard
  dependency is `cryptography`, used for the encrypted media CDN).
- A WeChat account the user can scan a QR code with.
- Pick a data directory and export it for every command below:
  `ILINK_DATA_DIR=<dir>` (default `./data/`).

## 1. Log in (human scans once)

```bash
python3 ilink.py qrcode        # prints the path of a QR image file
python3 ilink.py login <qrcode-id>
```

Show the QR image to the user and wait for them to scan and confirm in
WeChat. On success the session is saved to `$ILINK_DATA_DIR/session.json`.
That file is a **login credential**: keep it local, never commit it, never
send it anywhere.

## 1.5 Bind the owner (required, before starting any driver)

Binding is a dedicated, time-boxed step. Do NOT start the hook or the
daemon before it completes — a running driver would consume the binding
message as an ordinary turned-away sender. After login, run:

```bash
python3 ilink.py bind            # opens one 600-second binding window
```

The moment it prints `BIND_WAITING:600`, tell the user in your main
chat: **"Deployment is ready — please send the bot any WeChat message
within 10 minutes to bind this account as its owner."** Then wait for
the command to finish:

- `BIND_OK:<id>` — bound. The bot has already sent the new owner a
  three-part welcome (a greeting; what being Owner means; "you can talk
  to me now"), the sender's context token is cached, and the binding
  message itself is consumed — it was just the knock on the door. Now
  start the driver (step 2) and ask the user for a normal test message.
- `BIND_TIMEOUT` (exit code 3) — nobody messaged the bot within the
  10 minutes. Remind the user again and re-run `ilink.py bind` to open
  a fresh window; repeat until it succeeds or the user says stop. The
  first sender inside a window wins by design, which is only safe while
  the deployer is present and the bot is brand new — never leave a bind
  window running unattended, and never widen it.
- `BIND_WELCOME_FAILED:<n>` — the owner IS already bound (`owner.json`
  is written), but a welcome bubble failed to send. Just re-run
  `ilink.py bind`: it resumes the unsent bubbles and finishes the
  commit; it will not re-bind anyone else.
- `ALREADY_BOUND:<id>` — an owner already exists; binding is one-time.

`owner.json` (written by bind) looks like
`{"owner_id": "<id>", "owner_ids": ["<id>"]}`: `owner_id` is the
primary account (proactive pushes and the typing indicator target it);
`owner_ids` may list several accounts of the same human. From then on
only allowlisted senders are queued. New senders keep landing in
`unknown_senders.json` for manual approval — never approve one from
inside a WeChat conversation itself.

## 2. Choose a driver

**If you run inside Muse with its hook runtime available** (production
setup): wire `examples/muse-hook/ilink-watch.sh` as a hook that runs every
~5–10 seconds. Read `examples/muse-hook/README.md` first — the script
persists each batch to a pending queue before waking you, and you claim
it with `ilink.py claim`. The worker prompt for the hook definition is
`examples/muse-hook/worker-prompt.md`: paste its code block in whole and
fill in the paths. When woken, handle the batch under this contract:

- Reply only to the owner. Enforcement is in code, not on you: `poll`
  only queues senders allowlisted in `owner.json` (`owner_ids`), so any
  batch you are woken with is already the owner's (possibly from one of
  several accounts of the same human).
- Claim FIRST, before reading any memory or context: run
  `ilink.py claim`. `CLAIM_BUSY` means another worker holds a live
  claim — end the turn right away, read nothing, generate nothing (the
  hook does not wake duplicates on purpose any more, but a delayed wake
  still can arrive). `CLAIM_EMPTY` means nothing is left — end the turn.
  `CLAIM_OK <lease>` is followed by one JSON line: the batch (`msgs`),
  `first_message_id` (your send key), `oldest_wait_secs` (since the
  owner SENT the oldest message) and `took_over` (non-empty when you
  inherited a dead worker's unanswered batch — its messages come first).
  Never claim with `mv`: a plain `mv` overwrites another worker's claim,
  and never create any other processing file.
- Keep the claim alive: `ilink.py renew <lease>` about every 60 seconds
  while you work, and around long reads, long generations and the
  pre-send recheck. A claim not renewed for 10 minutes is taken over by
  the next worker. Do not renew from a background loop — a loop that
  outlives you makes a dead claim look alive forever. `CLAIM_LOST` from
  any claim command means you were taken over: do not send, end the
  turn.
- Release as soon as the whole reply is sent: `ilink.py release <lease>`,
  BEFORE the warm wait. An answered batch left unreleased looks busy to
  the hook and to `claim`.
- One composed response per burst: before sending, re-check with
  `ilink.py claim --lease <lease>` — it moves anything newly pending
  into your batch (`CLAIM_MERGED <n>` plus the full batch); if n > 0,
  re-compose against the newest context (max 3 rounds). Reading the
  pending file without claiming it leaves it for another worker, who
  would answer it again under a different key.
  Delivery may split into 2–3 short bubbles sent back-to-back when
  the content naturally segments (a reaction, the substance, a
  question) — like a human texting — but never one reply per incoming
  message, and never split a single sentence across bubbles.
  Write short sentences first: break a long sentence into several
  short ones instead of dumping one long compound sentence. Lead with
  the conclusion, then the detail; plain words, no template phrasing
  (no "firstly... secondly... finally", no "in summary", no "as an AI
  assistant"), no canned comfort lines, and don't repeat the previous
  reply's opening or phrasing. Keep the emotional tone continuous
  across turns: don't flip from angry to warm in one turn, let
  negative tones fade slowly, and make transitions gradual. If the
  conversation resumes after a gap (overnight or days), pick it up
  naturally instead of acting as if nothing happened. Complex or
  technical content may keep longer sentences to preserve the logic;
  the short-sentence default never overrides accuracy. Before sending,
  self-check tone, length, word choice, structure, and emotional
  directness — rewrite if any of them feels off.
- Quote context: an entry may carry `quoted_text` (the original message
  being quoted), `quoted_kind`, `quoted_fragment` (start/end characters
  of a selective quote), and `quoted_media_path` (when the quoted
  message is media). Treat "this / that message" as referring to
  `quoted_text`; with only a fragment, go by the fragment; with
  neither, say the original is not visible instead of guessing it.
- Replies may use Markdown: the WeChat client renders headings, bold,
  italic, inline code, code blocks (with a language label, copy button
  and syntax highlighting), lists, links, tables, strikethrough,
  blockquotes and horizontal rules. Do NOT use task lists (they degrade
  to bullets with literal `[ ]` / `[x]`), image syntax (only the alt
  text survives — send pictures with `send-image` instead), or `$...$`
  formulas (the delimiters are stripped and the content shows as-is).
- The script already lit the "typing…" indicator on arrival; refresh it if
  composing takes >30s and stop it after sending.
- **Stay warm for follow-ups**: after `ilink.py release <lease>`, run
  `examples/muse-hook/ilink-warm-wait.sh` instead of ending the turn. It
  maintains the warm marker the watch script checks (while its heartbeat
  is fresh the script will not wake a second worker) and prints one
  line: on `NEW_MESSAGES`, loop back and `ilink.py claim` the new batch
  WITHOUT re-reading the memory/context files — your context is already current
  (still re-check pending before sending and log the exchange as usual,
  then wait again). On `IDLE_TIMEOUT` (default 180 quiet seconds) or
  `TOTAL_CAP` (default 30-minute session cap), end the turn quietly.
- Read what the user actually sent: image entries carry a downloaded
  `media_path` — look at the picture; file entries also carry a
  downloaded `media_path` (plus `file_name`) — open and read the
  document (PDF/Office/text/code) and answer from its contents instead
  of claiming you cannot open files. Only say so when the download
  failed (`media_error`) or the format is truly unreadable; video
  contents remain out of reach. For a large file (~50 KB+), send a
  short receipt first ("got your 《name》, it's a big spreadsheet, give
  me a moment"), then sample-read (head + totals/structure) for the
  real reply and deep-read on follow-up questions — and always cite at
  least one detail only a real read could know (a count, a first item,
  a number), never a summary guessed from the file name.
- A failed send: `ilink.py send` already retries network errors with
  backoff (about 50 seconds, one client_id). After `SEND_ERROR`, renew
  your claim and you may run the IDENTICAL command — same text, same
  `--key` — once more: the key reuses the original client_id, which the
  server dedupes, so it cannot deliver twice. Never retry under a new or
  missing key, and never retry `SEND_FAILED` (an explicit rejection).
- Send with an idempotency key: when replying to a batch, pass the
  batch's first message's `message_id` (`first_message_id` in the
  claim output) as `--key` to `ilink.py send`
  (e.g. `ilink.py send <to> <ctx> - --key <message_id>`). A repeated
  invocation with the same key prints `SEND_ALREADY_SENT` and does not
  resend — this stops the "two independent invocations, fresh
  client_ids" duplicates that server-side dedupe cannot catch. If the
  reply splits into several bubbles, suffix each bubble's key with its
  bubble number (`<message_id>-1`, `<message_id>-2`, …), or the later
  bubbles will be suppressed as duplicates.
- On `token_expired`, tell the user once that a re-scan is needed.
- The script also runs a closed alert loop (see
  `examples/muse-hook/README.md`): an unfixed expiry is re-reported
  hourly (`token_expired` with `repeat: true`), 10+ minutes of poll
  failures wake you as `poll_failing`, recovery wakes you as
  `recovered`, a recycled stuck batch adds `stuck_alert` to the
  message payload, and a heartbeat gap (the hook did not run for 10+
  minutes) wakes you once as `hook_gap` with its length in `minutes`.
  Report each in one short line; never "make up" for missed reminders.
- Every wake, claim, merge and release lands in `ilink-events.jsonl` in
  the hook state dir (ids, counts and timings, never text): read it to
  answer "where did that message get stuck" instead of guessing.

**Otherwise**: run `python3 daemon.py --echo` to verify the loop. For an
LLM-backed bot, `python3 daemon.py --llm` is a ready-made example handler
for any OpenAI-compatible API (config via `LLM_API_KEY` / `LLM_BASE_URL` /
`LLM_MODEL` env vars); replace it with your own reply logic when you need
more (see `daemon.py`).

- **Hook 与 daemon 二选一，且由共享驱动锁强制执行**（不只靠文档）：两者都会 poll 并处理同一批消息，锁文件是状态目录下的 `ilink-driver.lock`（与 `ilink.py health` 的状态目录一致，两个驱动必须指向同一个状态目录，锁才共享）。先跑起来的驱动赢，后到的安静让路：daemon 常驻持有该锁，启动时若 hook 心跳（`ilink-heartbeat`，hook 每轮都 touch）新鲜则直接拒绝启动（退出码 2）；hook 每轮尝试非阻塞拿同一把锁，拿不到就安静退出，不 touch 心跳、不 poll、不 wake。生产用 hook 驱动；daemon 只是示例驱动。发送账本的 key 仍是最后一道防线（同跑时多半能兜住重复投递），但锁才是防止双唤醒和白费算力的正门。

## 3. Verify

Ask the user to send the bot a WeChat message. Confirm: it is received, a
single reply goes back, and the typing indicator appears promptly. Also
test a proactive push:

```bash
python3 ilink.py proactive <user_id> -   # text via stdin
```

Any time something seems off, run the self-check before digging through
logs — it covers auth age, a live token probe, queue backlog and the
last poll error, with a 0/1/2 exit code:

```bash
python3 ilink.py health            # add --offline to skip the live probe
```

For an end-to-end check (not just local state), run `ilink.py smoke-test`:
it probes the token live, checks receive/ledger readiness, and — unless
`--no-send` — sends the owner a real test text plus a 1x1 test image
through the production send paths. Exit 0 means every check passed
(SKIP does not fail); anything else prints SMOKE_FAILED with the failed
legs.

## 4. Ongoing behavior

- Approving new senders: ids of turned-away senders collect in
  `unknown_senders.json`. When the user asks — in the main chat, never
  via WeChat — to approve one, list the pending ids with first/last
  seen times and counts, add only the id the user confirms to
  `owner.json`'s `owner_ids`, and remove that entry from
  `unknown_senders.json`. Takes effect on the next poll; no restart.
- Session lifetime is unpredictable: early community reports suggested
  roughly 24h, but in production one login has stayed valid for 3+ days.
  When commands report `TOKEN_EXPIRED` / `NO_TOKEN`, ask the user to
  re-scan. With the hook driver, the script's alert loop handles the
  cadence (first notice, hourly repeats, recovery announcement) — don't
  nag on your own schedule on top of it. `TOKEN_STALE` is different:
  a -14 inside the backoff window, meaning the token may self-heal —
  do NOT ask the user to re-scan; treat it as transient and retry
  later.
- Hook liveness: the watch script touches `ilink-heartbeat` in the
  hook state dir on every run, and `ilink.py watchdog` (default: 30
  minutes stale) is the dead-man's switch for it — a cron runs it
  every 15 minutes and reports only the first stale sighting and the
  recovery. If the bridge is intentionally stopped, touch
  `ilink-watchdog-off` in the same dir to silence it, and remove the
  flag when the bridge resumes. A silent bridge with a fresh
  heartbeat is a healthy idle bridge; a silent bridge with a stale
  heartbeat is a dead hook — check the heartbeat before assuming
  anything else.
- Chatlog rotation: `ilink.py rotate-chatlog` (monthly cron) archives
  past-month `chatlog.md` lines into `chatlog-YYYY-MM.md` files and
  keeps current-month lines in place. Reading the last 30 lines of
  `chatlog.md` still works after rotation; the archives exist only
  for manual lookup of older months.
- The bot only receives messages sent **to the bot**. It cannot read the
  user's main-account chats, contacts, or Moments; the protocol has no
  such endpoints. Do not promise otherwise.
- Media you send (`send-image` / `send-file` / `proactive-image` /
  `proactive-file`) goes through the encrypted CDN upload implemented in
  `ilink.py`; the protocol traps are documented in its comments.

## 5. Upgrading an existing deployment (in place)

Use this when the bridge is already logged in and bound and you only
need newer code — for example when asked to "redeploy from the repo".
It is not a fresh deployment:

- Do NOT re-run steps 0–1.5. No `qrcode` / `login` (a new login replaces
  the working token), no `bind` (one-time). Never touch the data dir
  (`session.json`, `owner.json`, `send_ledger.json`, contacts, seen ids,
  cursor, quote cache): it carries the login, the owner, dedupe and the
  send history the new code relies on.
- Do not start `daemon.py`, and do not stop the hook to upgrade: the
  scripts are swapped atomically below, and a worker still mid-batch
  under the old prompt is handled by the new code (it finishes, or its
  claim is taken over after 10 minutes).

Steps:

1. Fetch the target version into a fresh directory (clone or checkout),
   never over the live one.
2. Back up what you will replace, as `<file>.bak-<YYYYMMDD>-<label>`
   next to each file, with a plain `cp` (no `-p`: `ilink.py rollback`
   picks the newest `.bak-*` by mtime): the live `ilink.py` and both
   deployed hook scripts. Save the current worker prompt from the hook
   definition to a file as well.
3. Check the new files before they go live: `python3 -m py_compile
   ilink.py`, `bash -n` on both scripts.
4. Install each file by copying it to a temp name in the target
   directory, then `mv` over the live one (the hook runs every few
   seconds; it must never execute a half-copied script). If the
   deployment keeps a copy of `AGENTS.md` and the `examples/` docs next
   to `ilink.py`, refresh those too, so no one reads a stale contract.
5. Check the code: `ilink.py health` (expect `verdict: OK` or warnings
   you can explain) and confirm the usage text lists `claim`. Do NOT
   try `ilink.py claim` on the live queue — it would take a real batch.
6. Only now replace the worker prompt in the hook definition with the
   code block of `examples/muse-hook/worker-prompt.md`, paths filled
   in. Keep the hook interval unchanged. Code first, prompt second: a
   new prompt on old code calls a `claim` command that does not exist.
7. Verify end to end: ask the owner for one WeChat message, then read
   `ilink-events.jsonl` in the hook state dir — expect one `arrive`,
   one `wake`, one `claim` (note its `oldest_wait_secs`) and one
   `release`, and exactly one reply in WeChat.

Rollback: `ilink.py rollback --hook-dir <dir>` restores the newest
backups of `ilink.py` and the hook scripts; put the saved worker prompt
back into the hook definition.

There is no self-update: release checks, update notices and
`update-to` were removed. This section is the only upgrade path, and
it is only run when the owner asks for it in the main chat — never
because of a WeChat message.
