#!/usr/bin/env python3
"""Regression tests for the 2026-11-03 reliability pass.

Production that morning: 236 wakes since midnight, about 95 for one batch,
30 of 34 worker turns spent re-checking work already done, wake delivery
backed up for over an hour, and a worker-improvised
ilink-processing-2.json nobody recovered. Each test below was first run
as an offline reproduction against the old code (all reproduced), then
fixed:

Hook (examples/muse-hook/ilink-watch.sh, against a fake ilink.py):
  * one wake per batch; an unclaimed batch is re-woken only after
    ILINK_REWAKE_SECS, not every round
  * an open, unanswered claim suppresses the wake (its worker merges
    pending before sending); an answered one does not
  * a stale claim that was already answered is dropped, not recycled
    into a fresh wake; stray ilink-processing-*.json files are folded
    back (unanswered) or dropped (answered)
  * typing runs in the background and never delays the wake
  * a heartbeat gap is reported once as hook_gap (not while the
    maintenance flag is set)
  * wakes land in ilink-events.jsonl

ilink.py:
  * poll: a non -14 business error is POLL_ERROR, not an empty batch
  * poll: create_time_ms reaches the queue entry
  * claim / renew / release: busy, merge, takeover, lost, stray files
  * send: network errors retry with backoff on one client_id
  * health: an old unclaimed batch and stray claim files are warnings

    python3 -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
DATA_DIR = tempfile.mkdtemp(prefix="ilink-test-rel-")
os.environ["ILINK_DATA_DIR"] = DATA_DIR
import ilink  # noqa: E402
from test_hook_integration import BATCH, EMPTY, SCRIPT_SRC  # noqa: E402

RUNTIME_STUB = """log() { echo "LOG $*" >> "$TRACE_CALLS"; }
silent() { echo "SILENT $*" >> "$TRACE_CALLS"; }
wake() { echo "WAKE $1 $2" >> "$TRACE_CALLS"; }
"""

FAKE_ILINK = r"""#!/usr/bin/env python3
import os, sys, time
trace = os.environ["TRACE_CALLS"]
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd == "typing":
    time.sleep(float(os.environ.get("FAKE_TYPING_DELAY", "0")))
with open(trace, "a") as f:
    f.write("ILINK " + " ".join(sys.argv[1:]) + "\n")
if cmd == "poll":
    print(os.environ.get("FAKE_OUT", ""))
elif cmd == "commit":
    sys.stdin.read()
    print("COMMIT_OK")
"""


def _msgs(*ids):
    return {"msgs": [{"from_user_id": "owner1", "message_id": i,
                      "kind": "text", "text": f"m{i}"} for i in ids]}


@unittest.skipUnless(shutil.which("jq") and shutil.which("bash"),
                     "needs bash + jq")
class HookWakeTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ilink-rel-hook-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.bridge = os.path.join(self.dir, "bridge")
        self.state = os.path.join(self.dir, "state")
        os.makedirs(self.bridge)
        os.makedirs(self.state)
        self.calls = os.path.join(self.dir, "calls.log")
        open(self.calls, "w").close()
        with open(os.path.join(self.bridge, "ilink.py"), "w") as f:
            f.write(FAKE_ILINK)
        with open(os.path.join(self.bridge, "owner.json"), "w") as f:
            json.dump({"owner_id": "owner1", "owner_ids": ["owner1"]}, f)
        self.runtime = os.path.join(self.dir, "runtime.sh")
        with open(self.runtime, "w") as f:
            f.write(RUNTIME_STUB)
        with open(SCRIPT_SRC) as f:
            # Same single test-only substitution as test_hook_integration:
            # ctime cannot be backdated, mtime can.
            script = f.read().replace("-cmin +10", "-mmin +10")
        self.script = os.path.join(self.dir, "watch.sh")
        with open(self.script, "w") as f:
            f.write(script)

    def p(self, name):
        return os.path.join(self.state, name)

    def write(self, name, obj, age_mins=None):
        with open(self.p(name), "w") as f:
            json.dump(obj, f)
        if age_mins is not None:
            subprocess.run(["touch", "-d", f"{age_mins} minutes ago",
                            self.p(name)], check=True)

    def mark_sent(self, key):
        with open(os.path.join(self.bridge, "send_ledger.json"), "w") as f:
            json.dump({key: {"client_id": "c", "at": int(time.time()),
                             "status": "sent", "result": "ok"}}, f)

    def run_hook(self, fake_out=EMPTY, extra_env=None):
        env = dict(os.environ)
        env.update({"ILINK_DATA_DIR": self.bridge,
                    "ILINK_HOOK_STATE_DIR": self.state,
                    "ILINK_HOOK_STATE": self.state,
                    "HATCH_HOOK_RUNTIME": self.runtime,
                    "HATCH_HOOK_DRY_RUN": "0",
                    "TRACE_CALLS": self.calls,
                    "FAKE_OUT": fake_out})
        env.update(extra_env or {})
        r = subprocess.run(["bash", self.script], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.calls) as f:
            return f.read()

    def wakes(self, calls):
        return [ln for ln in calls.splitlines() if ln.startswith("WAKE")]

    def test_unclaimed_batch_woken_once_then_rewoken_after_interval(self):
        self.write("ilink-pending.json", _msgs(1))
        for _ in range(4):
            calls = self.run_hook()
        self.assertEqual(len(self.wakes(calls)), 1, calls)
        self.assertIn("waiting for a claim", calls)
        self.assertNotIn("ILINK poll", calls)  # still queued: no new poll
        calls = self.run_hook(extra_env={"ILINK_REWAKE_SECS": "0"})
        self.assertEqual(len(self.wakes(calls)), 2, calls)

    def test_new_batch_resets_the_wake_throttle(self):
        self.write("ilink-pending.json", _msgs(1))
        self.run_hook()
        os.remove(self.p("ilink-pending.json"))  # claimed and released
        calls = self.run_hook(BATCH)
        self.assertEqual(len(self.wakes(calls)), 2, calls)
        self.assertIn("WAKE new_wechat_messages", calls)

    def test_open_unanswered_claim_suppresses_wake(self):
        self.write("ilink-processing.json", _msgs(1))
        self.write("ilink-pending.json", _msgs(2))
        for _ in range(3):
            calls = self.run_hook()
        self.assertEqual(self.wakes(calls), [])
        self.assertIn("merges pending before sending", calls)

    def test_new_batch_left_to_open_claim(self):
        self.write("ilink-processing.json", _msgs(1))
        calls = self.run_hook(BATCH)
        self.assertEqual(self.wakes(calls), [])
        self.assertIn("ILINK commit", calls)
        with open(self.p("ilink-pending.json")) as f:
            self.assertEqual(len(json.load(f)["msgs"]), 1)

    def test_answered_open_claim_does_not_suppress_wake(self):
        # Its worker already sent, so it will not merge any more pending;
        # the new batch needs a wake (claim will drop the answered one).
        self.write("ilink-processing.json", _msgs(1))
        self.mark_sent("1")
        self.write("ilink-pending.json", _msgs(2))
        calls = self.run_hook()
        self.assertEqual(len(self.wakes(calls)), 1, calls)

    def test_stale_answered_claim_dropped_without_wake(self):
        # Old contract had no release step: an answered batch aged past
        # 10 minutes and was recycled into a fresh (wasted) wake.
        self.write("ilink-processing.json", _msgs(1), age_mins=11)
        self.mark_sent("1-1")  # bubble keys count too
        calls = self.run_hook()
        self.assertFalse(os.path.exists(self.p("ilink-processing.json")))
        self.assertFalse(os.path.exists(self.p("ilink-pending.json")))
        self.assertEqual(self.wakes(calls), [])
        self.assertIn("already answered", calls)

    def test_stale_unanswered_claim_is_recycled_and_woken(self):
        self.write("ilink-processing.json", _msgs(1), age_mins=11)
        calls = self.run_hook()
        with open(self.p("ilink-pending.json")) as f:
            self.assertEqual(json.load(f), _msgs(1))
        self.assertEqual(len(self.wakes(calls)), 1, calls)

    def test_stray_processing_file_folded_or_dropped(self):
        self.write("ilink-pending.json", _msgs(3))
        self.write("ilink-processing-2.json", _msgs(2), age_mins=30)
        self.write("ilink-processing-3.json", _msgs(9), age_mins=30)
        self.write("ilink-processing-4.json", _msgs(8))  # fresh: in use
        self.mark_sent("9")
        self.run_hook()
        with open(self.p("ilink-pending.json")) as f:
            ids = [m["message_id"] for m in json.load(f)["msgs"]]
        self.assertEqual(ids, [2, 3])
        self.assertFalse(os.path.exists(self.p("ilink-processing-2.json")))
        self.assertFalse(os.path.exists(self.p("ilink-processing-3.json")))
        self.assertTrue(os.path.exists(self.p("ilink-processing-4.json")))

    def test_typing_never_delays_the_wake(self):
        t0 = time.time()
        calls = self.run_hook(BATCH, extra_env={"FAKE_TYPING_DELAY": "3"})
        self.assertLess(time.time() - t0, 2.5)
        self.assertIn("WAKE new_wechat_messages", calls)
        for _ in range(50):  # the background typing call still happens
            with open(self.calls) as f:
                if "ILINK typing owner1 start" in f.read():
                    break
            time.sleep(0.1)
        else:
            self.fail("typing never ran")

    def test_pending_rounds_do_not_resend_typing(self):
        self.write("ilink-pending.json", _msgs(1))
        for _ in range(4):
            self.run_hook()
        time.sleep(0.5)
        with open(self.calls) as f:
            self.assertEqual(f.read().count("ILINK typing"), 1)

    def test_heartbeat_gap_reported_once(self):
        hb = self.p("ilink-heartbeat")
        open(hb, "w").close()
        subprocess.run(["touch", "-d", "20 minutes ago", hb], check=True)
        calls = self.run_hook(BATCH)
        self.assertEqual(len(self.wakes(calls)), 1)
        self.assertIn("WAKE hook_gap", calls)
        self.assertNotIn("ILINK poll", calls)
        with open(os.path.join(self.bridge, "poll.log")) as f:
            self.assertIn("HOOK_GAP", f.read())
        calls = self.run_hook(BATCH)  # next round: plain processing
        self.assertEqual(calls.count("hook_gap"), 1)
        self.assertIn("WAKE new_wechat_messages", calls)

    def test_gap_silent_while_bridge_intentionally_stopped(self):
        hb = self.p("ilink-heartbeat")
        open(hb, "w").close()
        subprocess.run(["touch", "-d", "20 minutes ago", hb], check=True)
        open(self.p("ilink-watchdog-off"), "w").close()
        calls = self.run_hook()
        self.assertNotIn("hook_gap", calls)

    def test_wake_is_recorded_in_event_log(self):
        self.run_hook(BATCH)
        with open(self.p("ilink-events.jsonl")) as f:
            events = [json.loads(ln) for ln in f]
        kinds = [e["event"] for e in events]
        self.assertEqual(kinds, ["arrive", "wake"])
        self.assertEqual(events[1]["sig"], "7")
        self.assertNotIn("hi", json.dumps(events))  # never message text


class IlinkBase(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="ilink-rel-state-")
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"ILINK_HOOK_STATE_DIR": self.state})
        env.start()
        self.addCleanup(env.stop)
        ilink.BRIDGE = DATA_DIR
        ilink.SESSION_FILE = os.path.join(DATA_DIR, "session.json")
        ilink.CONTACTS_FILE = os.path.join(DATA_DIR, "contacts.json")
        ilink.SEEN_FILE = os.path.join(DATA_DIR, "seen_ids.json")
        ilink.SEND_AUDIT_FILE = os.path.join(DATA_DIR, "send_audit.jsonl")
        ilink.SEND_LEDGER_FILE = os.path.join(DATA_DIR, "send_ledger.json")
        ilink.SEND_LEDGER_LOCK_FILE = os.path.join(DATA_DIR, "send_ledger.lock")
        ilink.QUOTE_CACHE_FILE = os.path.join(DATA_DIR, "quote_cache.json")
        ilink.QUOTE_CACHE_LOCK_FILE = os.path.join(DATA_DIR, "quote_cache.lock")
        for name in ("send_ledger.json", "send_audit.jsonl", "seen_ids.json",
                     "quote_cache.json"):
            try:
                os.remove(os.path.join(DATA_DIR, name))
            except FileNotFoundError:
                pass
        with open(ilink.SESSION_FILE, "w") as f:
            json.dump({"bot_token": "tok", "cursor": "C0"}, f)
        with open(os.path.join(DATA_DIR, "owner.json"), "w") as f:
            json.dump({"owner_id": "owner1", "owner_ids": ["owner1"]}, f)
        with open(ilink.CONTACTS_FILE, "w") as f:
            json.dump({"owner1": {"context_token": "ctx"}}, f)

    def run_cmd(self, func, *args, **kwargs):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(io.StringIO()):
            code = func(*args, **kwargs)
        return code, buf.getvalue().strip().splitlines()

    def p(self, name):
        return os.path.join(self.state, name)

    def write(self, name, obj):
        with open(self.p(name), "w") as f:
            json.dump(obj, f)

    def read(self, name):
        with open(self.p(name)) as f:
            return json.load(f)

    def mark_sent(self, key):
        with open(ilink.SEND_LEDGER_FILE, "w") as f:
            json.dump({key: {"client_id": "c", "at": int(time.time()),
                             "status": "sent", "result": "ok"}}, f)

    def claim(self, lease=None):
        code, out = self.run_cmd(ilink.cmd_claim, lease)
        batch = json.loads(out[1]) if len(out) > 1 else None
        return code, out[0], batch


class PollTest(IlinkBase):
    def test_business_error_is_poll_error(self):
        with mock.patch.object(ilink, "api",
                               return_value={"code": 500, "errmsg": "internal"}):
            code, out = self.run_cmd(ilink.cmd_poll)
        self.assertEqual(code, 1)
        self.assertTrue(out[0].startswith("POLL_ERROR:code=500"), out)

    def test_create_time_ms_is_queued(self):
        raw = {"code": 0, "get_updates_buf": "C1", "msgs": [
            {"message_type": 1, "message_id": 9, "from_user_id": "owner1",
             "context_token": "ctx", "create_time_ms": 1762130379000,
             "item_list": [{"type": 1, "text_item": {"text": "hi"}}]},
            {"message_type": 1, "message_id": 10, "from_user_id": "owner1",
             "context_token": "ctx",
             "item_list": [{"type": 1, "create_time_ms": 1762130380000,
                            "text_item": {"text": "yo"}}]}]}
        with mock.patch.object(ilink, "api", return_value=raw):
            code, out = self.run_cmd(ilink.cmd_poll)
        self.assertEqual(code, 0)
        msgs = json.loads(out[0])["msgs"]
        self.assertEqual([m["create_time_ms"] for m in msgs],
                         [1762130379000, 1762130380000])


class ClaimTest(IlinkBase):
    def test_empty_queue(self):
        code, status, _ = self.claim()
        self.assertEqual((code, status), (0, "CLAIM_EMPTY"))

    def test_claim_takes_pending(self):
        q = _msgs(1, 2)
        q["msgs"][0]["create_time_ms"] = int((time.time() - 90) * 1000)
        self.write("ilink-pending.json", q)
        code, status, batch = self.claim()
        self.assertEqual(code, 0)
        self.assertTrue(status.startswith("CLAIM_OK "))
        lease = status.split()[1]
        self.assertEqual(batch["lease"], lease)
        self.assertEqual(batch["count"], 2)
        self.assertEqual(batch["first_message_id"], 1)
        self.assertGreaterEqual(batch["oldest_wait_secs"], 89)
        self.assertFalse(os.path.exists(self.p("ilink-pending.json")))
        self.assertEqual(self.read("ilink-processing.json")["lease"], lease)

    def test_second_worker_is_busy_and_leaves_pending_alone(self):
        self.write("ilink-pending.json", _msgs(1))
        self.claim()
        self.write("ilink-pending.json", _msgs(2))
        code, status, _ = self.claim()
        self.assertEqual(code, 0)
        self.assertTrue(status.startswith("CLAIM_BUSY"), status)
        self.assertEqual(self.read("ilink-pending.json"), _msgs(2))
        self.assertEqual(self.read("ilink-processing.json")["msgs"],
                         _msgs(1)["msgs"])

    def test_holder_merges_new_pending(self):
        self.write("ilink-pending.json", _msgs(1))
        _, status, _ = self.claim()
        lease = status.split()[1]
        self.write("ilink-pending.json", _msgs(1, 2))  # 1 redelivered
        code, status, batch = self.claim(lease)
        self.assertEqual((code, status), (0, "CLAIM_MERGED 1"))
        self.assertEqual([m["message_id"] for m in batch["msgs"]], [1, 2])
        self.assertEqual(batch["first_message_id"], 1)  # send key unchanged
        self.assertFalse(os.path.exists(self.p("ilink-pending.json")))

    def test_wrong_lease_is_lost(self):
        self.write("ilink-pending.json", _msgs(1))
        self.claim()
        code, status, _ = self.claim("not-mine")
        self.assertEqual((code, status), (1, "CLAIM_LOST"))
        self.assertEqual(self.run_cmd(ilink.cmd_renew, "not-mine")[0], 1)
        self.assertEqual(self.run_cmd(ilink.cmd_release, "not-mine")[0], 1)
        self.assertTrue(os.path.exists(self.p("ilink-processing.json")))

    def test_answered_claim_is_dropped_not_busy(self):
        self.write("ilink-pending.json", _msgs(1))
        self.claim()
        self.mark_sent("1")
        self.write("ilink-pending.json", _msgs(2))
        code, status, batch = self.claim()
        self.assertTrue(status.startswith("CLAIM_OK"), status)
        self.assertEqual([m["message_id"] for m in batch["msgs"]], [2])

    def test_stale_claim_taken_over_old_holder_locked_out(self):
        self.write("ilink-pending.json", _msgs(1))
        _, status, _ = self.claim()
        old = status.split()[1]
        self.write("ilink-pending.json", _msgs(2))
        with mock.patch.object(ilink, "CLAIM_STALE_SECS", 0):
            code, status, batch = self.claim()
        self.assertTrue(status.startswith("CLAIM_OK"), status)
        self.assertEqual(batch["took_over"], ["ilink-processing.json"])
        # The dead worker's messages lead: the send key stays its key.
        self.assertEqual([m["message_id"] for m in batch["msgs"]], [1, 2])
        self.assertEqual(self.run_cmd(ilink.cmd_renew, old)[1], ["CLAIM_LOST"])
        self.assertEqual(self.claim(old)[1], "CLAIM_LOST")

    def test_stray_claim_files(self):
        self.write("ilink-processing-2.json", _msgs(5))
        self.write("ilink-processing-3.json", _msgs(6))
        self.write("ilink-processing-4.json", _msgs(7))
        self.mark_sent("6")
        self.write("ilink-pending.json", _msgs(8))
        real_age = ilink._claim_age

        def age(path, now):
            name = os.path.basename(path)
            if name == "ilink-processing-4.json":
                return 5  # fresh: a pre-claim worker may be on it
            if name.startswith("ilink-processing-"):
                return 10_000
            return real_age(path, now)

        with mock.patch.object(ilink, "_claim_age", side_effect=age):
            _, status, batch = self.claim()
        self.assertTrue(status.startswith("CLAIM_OK"), status)
        self.assertEqual([m["message_id"] for m in batch["msgs"]], [5, 8])
        self.assertEqual(batch["took_over"], ["ilink-processing-2.json"])
        self.assertFalse(os.path.exists(self.p("ilink-processing-2.json")))
        self.assertFalse(os.path.exists(self.p("ilink-processing-3.json")))
        self.assertTrue(os.path.exists(self.p("ilink-processing-4.json")))

    def test_renew_and_release(self):
        self.write("ilink-pending.json", _msgs(1))
        _, status, _ = self.claim()
        lease = status.split()[1]
        self.assertEqual(self.run_cmd(ilink.cmd_renew, lease),
                         (0, ["RENEW_OK"]))
        self.assertEqual(self.run_cmd(ilink.cmd_release, lease),
                         (0, ["RELEASE_OK"]))
        self.assertFalse(os.path.exists(self.p("ilink-processing.json")))
        self.assertEqual(self.run_cmd(ilink.cmd_release, lease),
                         (0, ["RELEASE_GONE"]))

    def test_event_log_has_lifecycle_without_text(self):
        self.write("ilink-pending.json", _msgs(1))
        _, status, _ = self.claim()
        lease = status.split()[1]
        self.claim()  # busy
        self.run_cmd(ilink.cmd_release, lease)
        with open(self.p("ilink-events.jsonl")) as f:
            events = [json.loads(ln) for ln in f]
        self.assertEqual([e["event"] for e in events],
                         ["claim", "busy", "release"])
        self.assertNotIn("m1", json.dumps(events))

    def test_cli_dispatch(self):
        self.write("ilink-pending.json", _msgs(1))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(ilink.main(["ilink.py", "claim"]), 0)
        lease = buf.getvalue().split()[1]
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ilink.main(["ilink.py", "claim", "--lease"]), 2)
            self.assertEqual(ilink.main(["ilink.py", "renew"]), 2)
            self.assertEqual(ilink.main(["ilink.py", "release", lease]), 0)


class SendRetryTest(IlinkBase):
    def test_reconnects_within_backoff_on_one_client_id(self):
        # 2026-11-02 18:03: two RemoteDisconnected 5s apart lost a reply.
        seen = []

        def api(method, path, body=None, token=None, timeout=40):
            seen.append(body["msg"]["client_id"])
            if len(seen) < 3:
                raise ConnectionError("RemoteDisconnected")
            return {"code": 0, "message_id": 77}

        with mock.patch.object(ilink, "api", side_effect=api), \
                mock.patch.object(ilink.time, "sleep") as sleep:
            code, out = self.run_cmd(ilink.cmd_send, "owner1", "ctx",
                                     "reply", key="k1")
        self.assertEqual((code, out), (0, ["SEND_OK"]))
        self.assertEqual(len(set(seen)), 1)
        self.assertEqual([c.args[0] for c in sleep.call_args_list],
                         list(ilink.SEND_RETRY_BACKOFF[:2]))


class HealthBacklogTest(IlinkBase):
    def test_old_unclaimed_batch_is_a_warning(self):
        self.write("ilink-pending.json", _msgs(1))
        old = time.time() - 7200
        os.utime(self.p("ilink-pending.json"), (old, old))
        code, out = self.run_cmd(ilink.cmd_health, offline=True)
        self.assertEqual(code, 1)
        self.assertTrue(any("pending for 2h with no claim" in ln for ln in out), out)

    def test_fresh_batch_is_not(self):
        self.write("ilink-pending.json", _msgs(1))
        code, out = self.run_cmd(ilink.cmd_health, offline=True)
        self.assertEqual(code, 0, out)

    def test_stray_claim_file_is_a_warning(self):
        self.write("ilink-processing-2.json", _msgs(1))
        code, out = self.run_cmd(ilink.cmd_health, offline=True)
        self.assertEqual(code, 1)
        self.assertTrue(any("ilink-processing-2.json" in ln for ln in out), out)


if __name__ == "__main__":
    unittest.main()
