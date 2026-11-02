#!/usr/bin/env python3
"""Tests for the shared daemon/hook driver lock.

The "hook and daemon must not run together" rule used to live only in
AGENTS.md prose. Now it is a lock plus a heartbeat gate:

  * daemon.py: refuses to start when the hook heartbeat is fresh, and
    otherwise holds an exclusive flock on $STATE_DIR/ilink-driver.lock
    for its whole lifetime (a second daemon, or any holder of that
    lock, makes it exit with code 2).
  * examples/muse-hook/ilink-watch.sh: each run tries the same lock
    non-blockingly (fd 8, after its own single-instance lock on fd 9)
    and exits quietly — without touching the heartbeat, polling, or
    waking — when the daemon holds it.

    python3 -m unittest discover -s tests -v
"""
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import daemon  # noqa: E402

SCRIPT_SRC = os.path.join(ROOT, "examples", "muse-hook", "ilink-watch.sh")

RUNTIME_STUB = """# Test stub for the Muse hook runtime library (the script sources it).
log() { echo "LOG $*" >> "$TRACE_CALLS"; }
silent() { echo "SILENT $*" >> "$TRACE_CALLS"; }
wake() { echo "WAKE $1 $2" >> "$TRACE_CALLS"; }
"""

FAKE_ILINK = """#!/usr/bin/env python3
import json, os, sys
if len(sys.argv) > 1 and sys.argv[1] == "poll":
    print(os.environ.get("FAKE_OUT", ""))
"""

EMPTY = json.dumps({"msgs": [], "commit": {"cursor": "B9", "ids": []}})


@unittest.skipUnless(hasattr(fcntl, "flock"), "needs fcntl.flock")
class DriverLockUnitTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ilink-driver-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_hook_state_dir_env_precedence(self):
        old = dict(os.environ)
        try:
            os.environ.pop("ILINK_HOOK_STATE_DIR", None)
            os.environ.pop("ILINK_HOOK_STATE", None)
            self.assertEqual(daemon.hook_state_dir(),
                             os.path.expanduser("~/hooks/state"))
            os.environ["ILINK_HOOK_STATE"] = "/tmp/legacy-state"
            self.assertEqual(daemon.hook_state_dir(), "/tmp/legacy-state")
            os.environ["ILINK_HOOK_STATE_DIR"] = "/tmp/canonical-state"
            self.assertEqual(daemon.hook_state_dir(), "/tmp/canonical-state")
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_heartbeat_fresh_stale_missing(self):
        hb = os.path.join(self.dir, "ilink-heartbeat")
        self.assertFalse(daemon.hook_heartbeat_fresh(self.dir))
        open(hb, "w").close()
        now = time.time()
        os.utime(hb, (now, now))
        self.assertTrue(daemon.hook_heartbeat_fresh(self.dir, now=now))
        os.utime(hb, (now - 1000, now - 1000))
        self.assertFalse(daemon.hook_heartbeat_fresh(self.dir, now=now))
        # Boundary is inclusive: exactly at the stale window still counts.
        os.utime(hb, (now - 120, now - 120))
        self.assertTrue(daemon.hook_heartbeat_fresh(self.dir, now=now))

    def test_second_acquire_fails_while_first_held(self):
        path = os.path.join(self.dir, "ilink-driver.lock")
        first = daemon.try_acquire_driver_lock(path)
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(daemon.try_acquire_driver_lock(path))
        finally:
            first.close()
        # Releasing the first lock frees it for the next holder.
        second = daemon.try_acquire_driver_lock(path)
        self.assertIsNotNone(second)
        second.close()

    def test_acquire_missing_dir_raises_not_silent_none(self):
        # A missing directory must surface as an error (so the caller
        # can fall back) instead of masquerading as "lock held".
        with self.assertRaises(OSError):
            daemon.try_acquire_driver_lock(
                os.path.join(self.dir, "no-such-dir", "ilink-driver.lock"))


@unittest.skipUnless(shutil.which("jq") and shutil.which("bash"),
                     "needs bash + jq")
class HookDriverLockTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ilink-hook-driver-")
        self.bridge = os.path.join(self.dir, "bridge")
        self.state = os.path.join(self.dir, "state")
        os.makedirs(self.bridge)
        os.makedirs(self.state)
        self.calls = os.path.join(self.dir, "calls.log")
        open(self.calls, "w").close()
        with open(os.path.join(self.bridge, "ilink.py"), "w") as f:
            f.write(FAKE_ILINK)
        with open(os.path.join(self.bridge, "owner.json"), "w") as f:
            json.dump({"owner_id": "owner1"}, f)
        self.runtime = os.path.join(self.dir, "runtime.sh")
        with open(self.runtime, "w") as f:
            f.write(RUNTIME_STUB)
        with open(SCRIPT_SRC) as f:
            script = f.read().replace("-cmin +10", "-mmin +10")
        self.script = os.path.join(self.dir, "watch.sh")
        with open(self.script, "w") as f:
            f.write(script)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def run_hook(self):
        env = dict(os.environ)
        env.update({
            "ILINK_DATA_DIR": self.bridge,
            "ILINK_HOOK_STATE_DIR": self.state,
            "ILINK_HOOK_STATE": self.state,
            "HATCH_HOOK_RUNTIME": self.runtime,
            "HATCH_HOOK_DRY_RUN": "0",
            "TRACE_CALLS": self.calls,
            "FAKE_OUT": EMPTY,
        })
        r = subprocess.run(["bash", self.script], env=env,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        with open(self.calls) as f:
            return f.read()

    def test_hook_exits_quietly_when_daemon_holds_driver_lock(self):
        # A daemon holds the driver lock the way daemon.py does: the
        # hook run must do nothing at all — no wake, no poll side
        # effects, and crucially no heartbeat touch (a blocked hook
        # must not make the daemon think the hook is alive).
        lock_path = os.path.join(self.state, "ilink-driver.lock")
        with open(lock_path, "w") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            calls = self.run_hook()
        self.assertEqual(calls.strip(), "")
        self.assertFalse(os.path.exists(
            os.path.join(self.state, "ilink-heartbeat")))
        self.assertFalse(os.path.exists(
            os.path.join(self.state, "ilink-pending.json")))

    def test_hook_runs_and_touches_heartbeat_without_driver_lock(self):
        calls = self.run_hook()
        self.assertIn("SILENT", calls)  # empty batch: quiet, not a wake
        self.assertNotIn("WAKE", calls)
        self.assertTrue(os.path.exists(
            os.path.join(self.state, "ilink-heartbeat")))


if __name__ == "__main__":
    unittest.main()
