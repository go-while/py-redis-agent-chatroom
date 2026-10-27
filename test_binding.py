"""Session ownership regressions against a running, preferably private Redis.

Uses only a unique test prefix; no OpenCode, model requests or production room.
"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import chat


class Interleave:
    """Force a competing write after a legacy read, or before an atomic EVAL."""
    def __init__(self, redis, key, change, read="HGET"):
        self.redis, self.key, self.change, self.read = redis, key, change, read
        self.fired = False

    def __getattr__(self, name):
        return getattr(self.redis, name)

    def cmd(self, *args):
        legacy = args[0] == self.read and args[1] == self.key
        if not self.fired and (legacy or args[0] == "EVAL"):
            self.fired = True
            if legacy:
                result = self.redis.cmd(*args)
                self.change()
                return result
            self.change()
        return self.redis.cmd(*args)


class BindingTests(unittest.TestCase):
    def setUp(self):
        tag = hashlib.sha256(self._testMethodName.encode()).hexdigest()[:8]
        self.prefix = f"bindingtest-{os.getpid()}-{tag}-"
        self.host = os.environ.get("CHATROOM_HOST", "127.0.0.1")
        self.port = int(os.environ.get("CHATROOM_PORT", "6379"))
        self.db = int(os.environ.get("CHATROOM_DB", "7"))
        self.r = chat.Redis(self.host, self.port, self.db, os.environ.get("CHATROOM_PASSWORD"))
        self.base = [sys.executable, "-I", str(Path(chat.__file__).resolve()),
                     "--prefix", self.prefix, "--host", self.host,
                     "--port", str(self.port), "--db", str(self.db)]
        self.agent = self.prefix + "oc"
        self.sid = "ses_original1111"
        self.other = "ses_competing2222"

    def tearDown(self):
        self.r.cmd("DEL", *(self.prefix + name for name in ("agents", "cursor", "session", "chatroom")))
        self.r.sock.close()

    def register(self, agent, session, force=False):
        args = self.base + ["register", agent, "--session", session, "competitor"]
        if force:
            args.append("--force")
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def interleave(self, change, read="HGET"):
        return Interleave(self.r, self.prefix + "session", change, read)

    def main(self, redis, *args):
        with patch.object(chat, "Redis", return_value=redis), \
             patch.object(sys, "argv", ["chat.py", *self.base[3:], *args]), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                chat.main()
            except SystemExit as error:
                return error.code
        return 0

    def owner(self, agent=None):
        return self.r.cmd("HGET", self.prefix + "session", agent or self.agent)

    def start_hook(self, session, *flags):
        env = {k: v for k, v in os.environ.items()
               if k not in ("CHATROOM_ID", "CHATROOM_ID_PREFIX", "GROK_HOOK_EVENT", "GROK_SESSION_ID")}
        result = subprocess.run(self.base + list(flags) + ["hook-session-start"],
                                input=json.dumps(dict(session_id=session, source="resume")),
                                capture_output=True, text=True, timeout=10, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result.stdout

    def test_start_cannot_overwrite_concurrent_owner(self):
        r = self.interleave(lambda: self.register(self.agent, self.other))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            chat.session_start(r, self.prefix, self.agent, self.sid, {}, lambda _: None)
        self.assertEqual(self.owner(), self.other)
        self.assertIn("taken by another live session", out.getvalue())

    def test_start_cannot_add_second_name_after_stale_lookup(self):
        named = self.prefix + "assigned"
        self.register(named, self.sid)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            chat.session_start(self.r, self.prefix, self.agent, self.sid, {}, lambda _: None)
        self.assertIsNone(self.owner())
        self.assertEqual(chat.bound_ids(self.r, self.prefix, self.sid), [named])
        self.assertIn("you are agent " + named + ",", out.getvalue())

    def test_claude_resume_adopts_own_legacy_ownerless_derived_id(self):
        sid = "a6e51234-1111-2222-3333-444444444444"
        derived = self.prefix + chat.session_tag(sid)
        result = subprocess.run(self.base + ["register", derived, "legacy description"],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        description = self.r.cmd("HGET", self.prefix + "agents", derived)
        self.r.cmd("HSET", self.prefix + "cursor", derived, "123-4")
        out = self.start_hook(sid)
        self.assertIn("you are agent " + derived + ",", out)
        self.assertNotIn("without a recorded session owner", out)
        self.assertEqual(self.owner(derived), sid)
        self.assertEqual(self.r.cmd("HGET", self.prefix + "agents", derived), description)
        self.assertEqual(self.r.cmd("HGET", self.prefix + "cursor", derived), "123-4")

    def test_claude_resume_briefs_custom_binding_without_explicit_id(self):
        sid = "b7f62345-1111-2222-3333-444444444444"
        named = self.prefix + "claude.custom"
        derived = self.prefix + chat.session_tag(sid)
        self.register(named, sid)
        self.r.cmd("HSET", self.prefix + "cursor", named, "234-5")
        description = self.r.cmd("HGET", self.prefix + "agents", named)
        out = self.start_hook(sid)
        self.assertIn("you are agent " + named + ",", out)
        self.assertIn("register " + named + " --session " + sid, out)
        self.assertNotIn("refresh the briefing", out)
        self.assertEqual(chat.bound_ids(self.r, self.prefix, sid), [named])
        self.assertFalse(self.r.cmd("HEXISTS", self.prefix + "agents", derived))
        self.assertIsNone(self.r.cmd("HGET", self.prefix + "cursor", derived))
        self.assertEqual(self.r.cmd("HGET", self.prefix + "agents", named), description)
        self.assertEqual(self.r.cmd("HGET", self.prefix + "cursor", named), "234-5")

    def test_legacy_derived_adoption_honors_custom_id_prefix(self):
        sid = "c8a73456-1111-2222-3333-444444444444"
        prefix = self.prefix + "ge-"
        derived = prefix + chat.session_tag(sid)
        self.r.cmd("HSET", self.prefix + "agents", derived, "legacy tagged agent")
        out = self.start_hook(sid, "--id-prefix", prefix)
        self.assertIn("you are agent " + derived + ",", out)
        self.assertEqual(self.owner(derived), sid)
        self.assertEqual(self.r.cmd("HGET", self.prefix + "agents", derived), "legacy tagged agent")

    def test_explicit_fixed_id_is_not_derived_even_with_same_spelling(self):
        sid = "d9b84567-1111-2222-3333-444444444444"
        fixed = self.prefix + chat.session_tag(sid)
        self.r.cmd("HSET", self.prefix + "agents", fixed, "ownerless fixed agent")
        out = self.start_hook(sid, "--id", fixed)
        self.assertIn("without a recorded session owner", out)
        self.assertIsNone(self.owner(fixed))
        self.assertEqual(self.r.cmd("HGET", self.prefix + "agents", fixed), "ownerless fixed agent")

    def test_hook_leaves_ownerless_manual_id_alone(self):
        self.r.cmd("HSET", self.prefix + "agents", self.agent, "manual owner")
        with contextlib.redirect_stdout(io.StringIO()):
            chat.session_start(self.r, self.prefix, self.agent, self.sid, {}, lambda _: None)
        self.assertIsNone(self.owner())
        self.assertEqual(self.r.cmd("HGET", self.prefix + "agents", self.agent), "manual owner")

    def test_end_cannot_delete_reassigned_name(self):
        self.register(self.agent, self.sid)
        r = self.interleave(lambda: self.register(self.agent, self.other, force=True))
        chat.session_end(r, self.prefix, self.agent, self.sid, {}, lambda _: None)
        self.assertTrue(r.fired)
        self.assertEqual(self.owner(), self.other)
        self.assertTrue(self.r.cmd("HEXISTS", self.prefix + "agents", self.agent))

    def test_unregister_cannot_delete_reassigned_name(self):
        self.register(self.agent, self.sid)
        r = self.interleave(lambda: self.register(self.agent, self.other, force=True))
        code = self.main(r, "unregister", self.agent, "--session", self.sid)
        self.assertEqual(self.owner(), self.other)
        self.assertEqual(code, 1)

    def test_register_cannot_overwrite_concurrent_owner(self):
        r = self.interleave(lambda: self.register(self.agent, self.other))
        code = self.main(r, "register", self.agent, "--session", self.sid, "mine")
        self.assertEqual(self.owner(), self.other)
        self.assertEqual(code, 1)

    def test_register_cannot_bind_one_session_to_two_names(self):
        named = self.prefix + "assigned"
        r = self.interleave(lambda: self.register(named, self.sid), read="HGETALL")
        code = self.main(r, "register", self.agent, "--session", self.sid, "mine")
        self.assertEqual(chat.bound_ids(self.r, self.prefix, self.sid), [named])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
