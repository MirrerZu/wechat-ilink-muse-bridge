# wechat-ilink-muse-bridge

[![CI](https://github.com/MirrerZu/wechat-ilink-muse-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/MirrerZu/wechat-ilink-muse-bridge/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)

[中文](README.md) | [English](README.en.md)

**Connect Muse to WeChat.**

## About

Built on Tencent's official iLink protocol (`ilinkai.weixin.qq.com`). After a QR
scan, you message the bot in WeChat and a Muse worker picks it up and replies;
Muse can also push to you on its own. Text, images, files and video go both
ways, voice inbound only; only allowlisted owners can wake it.

The production driver is a Muse hook (`examples/muse-hook/`):

```
WeChat ──> iLink server ──long poll──> ilink-watch.sh (Muse hook, every 5 s)
                                           │ batch queued in ilink-pending.json
                                           ▼
                                   wake worker ──> ilink.py claim
                                           │       batch + last 24 h + today's diary
                                           ▼
                                   ilink.py send --lease ──> WeChat
                                           │       merges anything new before sending
                                           ▼
                                   release ──> ilink-warm-wait.sh waits for a follow-up
```

`ilink.py` is a framework-free iLink client: send and receive, queue claims,
media encryption, self-checks. This repository is a fork of the upstream
wechat-ilink-muse-bridge; since 2026-11-03 its reliability and reply latency
have been reworked against production data.

## What changed

### One worker per batch

- The hook wakes a batch once. No wake while a warm worker or an open claim
  exists; a batch nobody claimed is re-woken only after 180 seconds.
- Claims go through `ilink.py claim / renew / release`, locked and leased,
  instead of a bare `mv`. A duplicate worker gets `CLAIM_BUSY` and exits; a
  claim not renewed for 10 minutes is taken over, and one already answered is
  dropped instead of waking a worker to check it.
- Sends carry an idempotency key (the batch's first `message_id`), so one key
  never sends twice; network errors retry with backoff under one `client_id`.
- Self-update is gone. Upgrades are done in place by hand, per
  [`AGENTS.md`](AGENTS.md) section 5, with backups and rollback.

### Fewer model rounds

- `claim --recent --with <today's diary>`: a cold worker gets the batch, both
  sides' messages from the last 24 hours verbatim and the tail of today's
  diary in one call, instead of reading memory over several rounds.
- `send --lease`: the pre-send re-check runs inside the send. If something new
  arrived, nothing is sent; it prints `SEND_DEFERRED` and the merged batch, and
  the worker rewrites against the newest context.
- `ilink-warm-wait.sh` claims a follow-up for the waiting worker and hands it
  over with the batch.
- `send --lease` and `release` raise the warm marker, so a follow-up landing
  while the worker writes its diary no longer wakes a second, cold worker.

### Faster pickup

- A round's images and files download in parallel (up to 4 at a time).
- The background "typing…" child no longer holds the hook's locks; the round
  after every batch used to be skipped.
- A round with a queued batch that needs no wake still polls, and new messages
  are appended under the queue lock. Messages sent while the worker composed
  used to wait on the server until it finished.
- The hook runs every 5 seconds.

### Deployment and observability

- The hook scripts first read `ilink-hook.env` next to themselves, so runtimes
  whose hook definition has no environment field (a venv python, say) install
  the new scripts unchanged; upgrades never replace the file.
- `ilink-events.jsonl` records each batch's arrival, wake, claim, merge and
  release (ids, counts and times, never text). Arrivals carry `poll_secs` and
  `lag_secs`; `ilink.py stats` sums up today's pickup lag and claim wait.

## Results

Measured in production on 2026-11-03:

| Metric | Before | After |
|---|---|---|
| Wakes | 236 wakes in a day, one batch woken ~95 times; 30 of 34 worker turns re-checked finished work | 1 per batch, never a second worker |
| Cold reply (arrival → first bubble) | ~36 s per worker turn (bridge tools 0.4 s; the rest 3–9 s model rounds) | 16 s |
| Follow-up while the worker waits | — | 13 s |
| Several photos reaching the queue | 4 photos, downloaded one by one: 9 s | 10 photos (2.4 MB) in parallel: 4.3 s |
| Messages sent while the worker composes | a text arrived 17 s late and forced one more rewrite | 0–9 s |
| Pickup lag / claim wait (median) | — | 0 s / 1 s |

Open items (the first model round of a cold start varies; a photo burst gets
one draft before the rewrite) are tracked in
[`docs/latency-followups.md`](docs/latency-followups.md) (Chinese).

## Usage

### Fresh install

Send this to your Muse:

```
Use wechat-ilink-muse-bridge to connect WeChat to yourself: https://github.com/MirrerZu/wechat-ilink-muse-bridge
```

It deploys itself by following [`AGENTS.md`](AGENTS.md). You do two things:

1. Scan the QR code it shows you with WeChat.
2. When it asks, **send the bot any message within 10 minutes** to bind
   yourself as owner. The first sender becomes the Owner and gets a
   three-part welcome; if nobody writes in time, it reminds you and opens a
   new window.

### Upgrading an existing deployment

In Muse's **main chat**, say:

```
Upgrade the WeChat bridge in place to the latest main, following section 5 of AGENTS.md in https://github.com/MirrerZu/wechat-ilink-muse-bridge.
```

No re-scan and no re-bind; the data dir (login, owner, send history) stays as
it is. A bridge installed from the upstream repo upgrades the same way. Only
the main chat can start an upgrade, never a WeChat message.

### Day to day

- **Chat**: just message the bot. A burst of messages gets one reply; quote a
  message and ask about it, and it sees the quoted original.
- **Proactive push**: `python3 ilink.py proactive <user_id> -`, text on stdin.
- **Approving a new sender**: messages from senders outside the allowlist (your
  second WeChat account, say) are ignored and their ids land in
  `data/unknown_senders.json`. To let one in, tell Muse in the main chat to add
  the pending senders to the allowlist; it lists them for you to confirm.
  Only the main chat counts, never anything said in WeChat.
- **Re-scanning**: when the login expires, Muse tells you once, then hourly
  until you scan again.
- **When something seems off**: have Muse run `ilink.py health` first.

### Command cheat sheet

```bash
python3 ilink.py health [--offline]       # self-check: login, queue, last poll (exit 0/1/2)
python3 ilink.py stats                    # local snapshot: today's arrivals, pickup lag, claim wait, sends
python3 ilink.py smoke-test [--no-send]   # end-to-end check; sends the owner a test text and image
python3 ilink.py watchdog                 # hook heartbeat watchdog (cron every 15 minutes)
python3 ilink.py proactive <user_id> -    # push text
python3 ilink.py proactive-image <user_id> <image>    # push an image
python3 ilink.py rollback [--hook-dir <dir>]          # restore from the newest .bak-* backups
python3 ilink.py prune-media              # media cleanup (default: keep 30 days, 500 MB)
python3 ilink.py preflight                # pre-login environment check
```

The data dir defaults to `./data/`; set `ILINK_DATA_DIR` to change it. Run
`python3 ilink.py` for every command.

### Notes

- `data/session.json` is a **login credential**, like a password: keep it
  local, never commit or share it.
- The bot only receives messages sent to it; it cannot see your main
  account's chats, contacts or Moments.
- The iLink channel does not deliver voice messages the bot sends; send audio
  as a file instead.
  
## Acknowledgments

The human-like reply polish borrows ideas — not code — from the
following projects (what was *not* taken is noted too, so nobody
mistakes this for a copy):

- [ElmerAlan-an/human_chat_operit](https://github.com/ElmerAlan-an/human_chat_operit) — short colloquial sentences, a reply self-checklist, anti-repetition; its slash-command system and persona roleplay were not adopted
- [oaa529/zhichi](https://github.com/oaa529/zhichi) — a realism engine's message splitting, typing rhythm, and emotional continuity; its deliberate typos and take-backs were not adopted
- [huihuihenqiang/WeClone-llm](https://github.com/huihuihenqiang/WeClone-llm) — sentence-by-sentence replies; its hard punctuation splitting (which strips punctuation) was not adopted
- [Napus-BackendDev/GenieAi-ChatBOT](https://github.com/napus-backenddev/genieai-chatbot/blob/HEAD/CLAUDE.md) — separator-based bubble splitting plus length-scaled pauses in its dispatcher; the pause tiers are not implemented yet, reference only
- [freestylefly/openclaw-wechat](https://deepwiki.com/freestylefly/openclaw-wechat/4.5-reply-dispatcher) — Reply Dispatcher chunking and delivery timing
- [molang163/chinese-natural-reply](https://github.com/molang163/chinese-natural-reply) — conclusion first, plain language, no defensive hedging
- [speak-human-tw](https://www.typingmind.com/skills/raymondhou0917-speak-human-tw) — facts first, then de-AI the wording, then add human flavor
- [Treasure-hub-agent/ai-with-u](https://github.com/Treasure-hub-agent/ai-with-u) — time-gap awareness: resume naturally after a night or days away
- [kroxchan/xinyi](https://github.com/kroxchan/xinyi) — an emotion state machine (no abrupt emotional flips, negative tones fade slowly, gradual transitions) and a five-dimension authenticity self-check; its learn-a-person / decrypt-chat-history approach was not adopted

The protocol itself comes from Tencent's official iLink / openclaw-weixin
ecosystem. WeChat decryption, desktop automation, and chat-history
fine-tuning clones were all deliberately not used.


## License

MIT
