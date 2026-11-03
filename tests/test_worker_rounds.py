#!/usr/bin/env python3
"""Tests for the 2026-11-03 cut in worker model rounds.

Measured that day: the bridge tools took 0.4s of a 36s turn, and each
extra tool round cost the worker 3-9s of model time. Each command
below folds a round into a call the worker makes anyway:

  * claim --recent / --with: a cold worker gets the last 24h of
    exchanges (from the quote cache, verbatim, oldest first, minus the
    batch) and its diary tail with the claim, on CLAIM_OK only — no
    separate round to decide what to read
  * send --lease: the pre-send re-check runs inside the send;
    SEND_DEFERRED <n> + batch sends nothing; success raises the warm
    marker; a retry under a key the ledger knows checks the lease but
    merges nothing
  * ilink-warm-wait.sh claims for the worker (see test_warm_wait)

    python3 -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import ilink  # noqa: E402
from test_reliability_20261103 import IlinkBase, _msgs  # noqa: E402


class ClaimContextTest(IlinkBase):
    def setUp(self):
        super().setUp()
        self.files = tempfile.mkdtemp(prefix="ilink-rounds-files-")
        self.addCleanup(shutil.rmtree, self.files, ignore_errors=True)

    def cache(self, entries):
        with open(ilink.QUOTE_CACHE_FILE, "w") as f:
            json.dump({"by_id": entries, "by_svr": {}}, f)

    def claim_out(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(io.StringIO()):
            code = ilink.main(["ilink.py", "claim", *args])
        return code, buf.getvalue().splitlines()

    def test_recent_is_verbatim_oldest_first_without_the_batch(self):
        now = int(time.time())
        self.cache({
            "10": {"text": "早", "kind": "text", "from": "user",
                   "at": now - 300, "create_time_ms": (now - 302) * 1000},
            # A bot echo lands a poll later than the reply was sent: its
            # own send time, not the cache write time, decides the order.
            "11": {"text": "早呀", "kind": "text", "from": "bot",
                   "at": now - 200, "create_time_ms": (now - 301) * 1000},
            "12": {"text": "x" * 600, "kind": "text", "from": "user",
                   "at": now - 100},
            "13": {"text": "[图片]", "kind": "image", "from": "user",
                   "at": now - 50, "media_path": "/m/a.jpg"},
            "1": {"text": "m1", "kind": "text", "from": "user", "at": now},
            "9": {"text": "old", "kind": "text", "from": "user",
                  "at": now - 2 * 86400},
        })
        self.write("ilink-pending.json", _msgs(1))
        code, out = self.claim_out("--recent")
        self.assertEqual(code, 0)
        self.assertTrue(out[0].startswith("CLAIM_OK"))
        batch = json.loads(out[1])
        recent = batch["recent"]
        self.assertEqual([r["text"][:2] for r in recent],
                         ["早", "早呀", "xx", "[图"])
        self.assertEqual([r["from"] for r in recent],
                         ["owner", "bot", "owner", "owner"])
        self.assertEqual(len(recent[2]["text"]), ilink.RECENT_TEXT_MAX + 1)
        self.assertEqual(recent[3]["kind"], "image")
        self.assertEqual(recent[3]["media_path"], "/m/a.jpg")
        self.assertIn("recent_tz", batch)
        # The batch itself comes last, after its context
        self.assertEqual(list(batch)[-1], "msgs")

    def test_recent_keeps_the_newest(self):
        now = int(time.time())
        self.cache({str(100 + i): {"text": f"t{i}", "from": "user",
                                   "at": now - 1000 + i}
                    for i in range(ilink.RECENT_MAX + 5)})
        self.write("ilink-pending.json", _msgs(1))
        _, out = self.claim_out("--recent")
        recent = json.loads(out[1])["recent"]
        self.assertEqual(len(recent), ilink.RECENT_MAX)
        self.assertEqual(recent[-1]["text"], f"t{ilink.RECENT_MAX + 4}")

    def test_no_context_unless_asked(self):
        self.write("ilink-pending.json", _msgs(1))
        _, out = self.claim_out()
        self.assertEqual(len(out), 2)
        self.assertNotIn("recent", json.loads(out[1]))

    def test_with_appends_file_tails(self):
        diary = os.path.join(self.files, "today.md")
        with open(diary, "w") as f:
            f.write("".join(f"- entry {i}\n" for i in range(2000)))
        small = os.path.join(self.files, "brief.md")
        with open(small, "w") as f:
            f.write("主人喜欢短句\n")
        self.write("ilink-pending.json", _msgs(1))
        code, out = self.claim_out("--with", diary, small,
                                   os.path.join(self.files, "nope.md"))
        self.assertEqual(code, 0)
        text = "\n".join(out[2:])
        head = out[2]
        self.assertTrue(head.startswith(f"--- FILE {diary} (last "), head)
        self.assertEqual(out[3], out[3].lstrip())
        self.assertTrue(out[3].startswith("- entry "))  # cut at a line start
        self.assertIn("- entry 1999", text)
        self.assertNotIn("- entry 0\n", text + "\n")
        self.assertIn(f"--- FILE {small} ---\n主人喜欢短句\n--- END FILE ---",
                      text)
        self.assertIn(": missing ---", text)

    def test_with_refuses_the_login_credential(self):
        self.write("ilink-pending.json", _msgs(1))
        _, out = self.claim_out("--with", ilink.SESSION_FILE)
        text = "\n".join(out)
        self.assertIn("refused (login credential)", text)
        self.assertNotIn("tok", text.split("\n", 2)[2])

    def test_busy_or_empty_gets_no_context(self):
        diary = os.path.join(self.files, "today.md")
        with open(diary, "w") as f:
            f.write("secret diary\n")
        _, out = self.claim_out("--recent", "--with", diary)
        self.assertEqual(out, ["CLAIM_EMPTY"])
        self.write("ilink-processing.json",
                   {"lease": "held", "claimed_at": int(time.time()),
                    "msgs": _msgs(1)["msgs"]})
        self.write("ilink-pending.json", _msgs(2))
        _, out = self.claim_out("--recent", "--with", diary)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("CLAIM_BUSY"))

    def test_bad_options_are_usage_errors(self):
        self.write("ilink-pending.json", _msgs(1))
        self.assertEqual(self.claim_out("--recnt")[0], 2)
        self.assertEqual(self.claim_out("--lease")[0], 2)
        # Nothing was claimed by the rejected calls
        self.assertTrue(os.path.exists(self.p("ilink-pending.json")))


class SendLeaseTest(IlinkBase):
    def setUp(self):
        super().setUp()
        self.write("ilink-pending.json", _msgs(1))
        _, status, _ = self.claim()
        self.lease = status.split()[1]
        self.calls = []

    def send(self, lease):
        def api(method, path, body=None, token=None, timeout=40):
            self.calls.append(body)
            return {"code": 0, "message_id": 77}

        buf = io.StringIO()
        with mock.patch.object(ilink, "api", side_effect=api), \
                contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(io.StringIO()):
            code = ilink.main(["ilink.py", "send", "owner1", "ctx", "reply",
                               "--key", "1", "--lease", lease])
        return code, buf.getvalue().splitlines()

    def events(self):
        with open(self.p("ilink-events.jsonl")) as f:
            return [json.loads(ln) for ln in f]

    def test_no_new_messages_sends_and_raises_marker(self):
        code, out = self.send(self.lease)
        self.assertEqual((code, out), (0, ["SEND_OK"]))
        self.assertEqual(len(self.calls), 1)
        marker = self.read("ilink-warm.json")
        self.assertGreaterEqual(marker["heartbeat"], int(time.time()) - 5)
        merge = [e for e in self.events() if e["event"] == "merge"]
        self.assertEqual([(e["added"], e["count"]) for e in merge], [(0, 1)])

    def test_new_messages_defer_the_send(self):
        self.write("ilink-pending.json", _msgs(2))
        code, out = self.send(self.lease)
        self.assertEqual(code, 3)
        self.assertEqual(out[0], "SEND_DEFERRED 1")
        batch = json.loads(out[1])
        self.assertEqual([m["message_id"] for m in batch["msgs"]], [1, 2])
        self.assertEqual(batch["first_message_id"], 1)  # key unchanged
        self.assertEqual(self.calls, [])
        self.assertFalse(os.path.exists(self.p("ilink-pending.json")))
        self.assertFalse(os.path.exists(self.p("ilink-warm.json")))
        # Re-composed, the next send goes out
        self.assertEqual(self.send(self.lease)[0], 0)
        self.assertEqual(len(self.calls), 1)

    def test_retry_after_send_error_does_not_merge(self):
        # The first attempt may have been delivered: a re-composed text
        # under the same key would be deduped away, leaving the merged
        # message unanswered. It stays pending instead.
        def flaky(method, path, body=None, token=None, timeout=40):
            self.calls.append(body)
            raise ConnectionError("RemoteDisconnected")

        with mock.patch.object(ilink, "api", side_effect=flaky), \
                mock.patch.object(ilink.time, "sleep"), \
                contextlib.redirect_stdout(io.StringIO()):
            code = ilink.main(["ilink.py", "send", "owner1", "ctx", "reply",
                               "--key", "1", "--lease", self.lease])
        self.assertEqual(code, 1)  # SEND_ERROR
        self.write("ilink-pending.json", _msgs(2))
        self.calls = []
        code, out = self.send(self.lease)
        self.assertEqual((code, out), (0, ["SEND_OK"]))
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(os.path.exists(self.p("ilink-pending.json")))
        self.assertEqual(len(self.read("ilink-processing.json")["msgs"]), 1)

    def test_lost_claim_sends_nothing(self):
        code, out = self.send("not-mine")
        self.assertEqual((code, out), (1, ["CLAIM_LOST"]))
        self.assertEqual(self.calls, [])
        self.assertFalse(os.path.exists(self.p("ilink-warm.json")))

    def test_without_lease_nothing_changes(self):
        self.write("ilink-pending.json", _msgs(2))
        buf = io.StringIO()
        with mock.patch.object(ilink, "api",
                               return_value={"code": 0, "message_id": 78}), \
                contextlib.redirect_stdout(buf):
            code = ilink.main(["ilink.py", "send", "owner1", "ctx", "reply",
                               "--key", "1"])
        self.assertEqual((code, buf.getvalue().strip()), (0, "SEND_OK"))
        self.assertTrue(os.path.exists(self.p("ilink-pending.json")))
        self.assertFalse(os.path.exists(self.p("ilink-warm.json")))


if __name__ == "__main__":
    unittest.main()
