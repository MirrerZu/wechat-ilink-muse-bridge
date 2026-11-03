#!/usr/bin/env python3
"""Tests for examples/muse-hook/ilink-warm-wait.sh.

The warm waiter keeps a worker alive briefly after a reply so a
follow-up message skips the cold start. It owns the warm marker the
watch script checks. Its exits are pinned:

  * IDLE_TIMEOUT  quiet for ILINK_WARM_IDLE seconds, marker removed
  * CLAIM_OK      a batch appears in pending and the waiter claims it
                  for the worker (real ilink.py claim output), marker
                  kept (its stale heartbeat re-arms the hook if the
                  worker dies)
  * NEW_MESSAGES  a batch appears but ilink.py is missing or fails —
                  the worker claims itself
  * TOTAL_CAP     the session's total cap is already in the past
                  (marker left by a dead session) — exits at once

CLAIM_BUSY keeps it waiting without claiming again for a while;
CLAIM_EMPTY keeps it waiting.

    python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
HELPER = os.path.join(ROOT, "examples", "muse-hook", "ilink-warm-wait.sh")
REAL_CLIENT = os.path.join(ROOT, "ilink.py")

# Stand-in client: answers `claim` with FAKE_CLAIM and counts the calls.
FAKE_CLIENT = """import os, sys
with open(os.environ["FAKE_CALLS"], "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
print(os.environ["FAKE_CLAIM"])
"""


@unittest.skipUnless(shutil.which("jq") and shutil.which("bash"),
                     "needs bash + jq")
class WarmWaitTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="ilink-warm-")
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)
        self.env = dict(os.environ)
        self.env.update({"ILINK_HOOK_STATE_DIR": self.state,
                         "ILINK_DATA_DIR": self.state,
                         "ILINK_CLIENT": os.path.join(self.state, "absent.py"),
                         "ILINK_WARM_IDLE": "2", "ILINK_WARM_POLL": "1",
                         "ILINK_WARM_TOTAL": "1800"})

    def run_helper(self, timeout=15):
        return subprocess.run(["bash", HELPER], env=self.env,
                              capture_output=True, text=True,
                              timeout=timeout)

    def marker(self):
        p = os.path.join(self.state, "ilink-warm.json")
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return json.load(f)

    def test_idle_timeout_removes_marker(self):
        r = self.run_helper()
        self.assertEqual(r.stdout.strip(), "IDLE_TIMEOUT")
        self.assertIsNone(self.marker())

    def land_batch_while_waiting(self):
        # The deadline is counted in whole seconds, so an idle of N gives
        # a real window of N-1..N seconds. With 2, a start late in a second
        # timed out at ~1.0s, before the batch below lands at 1.2s.
        self.env["ILINK_WARM_IDLE"] = "5"
        proc = subprocess.Popen(["bash", HELPER], env=self.env,
                                stdout=subprocess.PIPE, text=True)
        time.sleep(1.2)
        with open(os.path.join(self.state, "ilink-pending.json"), "w") as f:
            json.dump({"msgs": [{"message_id": 1, "text": "hi"}]}, f)
        out, _ = proc.communicate(timeout=20)
        return out

    def fake_client(self, answer):
        path = os.path.join(self.state, "fake_ilink.py")
        with open(path, "w") as f:
            f.write(FAKE_CLIENT)
        self.calls = os.path.join(self.state, "calls")
        self.env.update({"ILINK_CLIENT": path, "FAKE_CLAIM": answer,
                         "FAKE_CALLS": self.calls})

    def claim_calls(self):
        if not os.path.exists(self.calls):
            return 0
        with open(self.calls) as f:
            return len(f.readlines())

    def test_claims_the_batch_for_the_worker(self):
        self.env["ILINK_CLIENT"] = REAL_CLIENT
        out = self.land_batch_while_waiting().splitlines()
        self.assertTrue(out[0].startswith("CLAIM_OK "), out)
        batch = json.loads(out[1])
        self.assertEqual(batch["first_message_id"], 1)
        self.assertEqual(batch["lease"], out[0].split()[1])
        self.assertFalse(os.path.exists(
            os.path.join(self.state, "ilink-pending.json")))
        with open(os.path.join(self.state, "ilink-processing.json")) as f:
            self.assertEqual(json.load(f)["lease"], batch["lease"])
        self.assertIsNotNone(self.marker())

    def test_no_client_falls_back_to_new_messages(self):
        out = self.land_batch_while_waiting()
        self.assertEqual(out.strip(), "NEW_MESSAGES")
        self.assertIsNotNone(self.marker())

    def test_failing_client_falls_back_to_new_messages(self):
        self.fake_client("Traceback (most recent call last):")
        self.assertEqual(self.land_batch_while_waiting().strip(),
                         "NEW_MESSAGES")

    def test_busy_keeps_waiting_and_backs_off(self):
        self.fake_client("CLAIM_BUSY 3")
        self.env["ILINK_WARM_IDLE"] = "4"
        with open(os.path.join(self.state, "ilink-pending.json"), "w") as f:
            json.dump({"msgs": [{"message_id": 1}]}, f)
        r = self.run_helper()
        self.assertEqual(r.stdout.strip(), "IDLE_TIMEOUT")
        self.assertEqual(self.claim_calls(), 1)

    def test_empty_keeps_waiting(self):
        self.fake_client("CLAIM_EMPTY")
        with open(os.path.join(self.state, "ilink-pending.json"), "w") as f:
            json.dump({"msgs": [{"message_id": 1}]}, f)
        r = self.run_helper()
        self.assertEqual(r.stdout.strip(), "IDLE_TIMEOUT")
        self.assertGreaterEqual(self.claim_calls(), 1)

    def test_dead_session_marker_hits_total_cap(self):
        now = int(time.time())
        with open(os.path.join(self.state, "ilink-warm.json"), "w") as f:
            json.dump({"since": now - 3600, "activity": now - 3600,
                       "heartbeat": now - 3600, "until": now - 1800}, f)
        r = self.run_helper()
        self.assertEqual(r.stdout.strip(), "TOTAL_CAP")
        self.assertIsNone(self.marker())


if __name__ == "__main__":
    unittest.main()
