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
        self.r.cmd("DEL", *(self.prefix + name for name in ("agents", "cursor", "session", "session-kind", "last-seen", "model", "chatroom")))
        self.r.sock.close()

    def register(self, agent, session, force=False, model=None):
        args = self.base + ["register", agent, "--session", session, "competitor"]
        if model:
            args += ["--model", model]
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

    def start_hook(self, session, *flags, model=None):
        env = {k: v for k, v in os.environ.items()
               if k not in ("CHATROOM_ID", "CHATROOM_ID_PREFIX", "GROK_HOOK_EVENT", "GROK_SESSION_ID")}
        payload = dict(session_id=session, source="resume")
        if model:
            payload["model"] = model
        result = subprocess.run(self.base + list(flags) + ["hook-session-start"],
                                input=json.dumps(payload),
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
        self.assertIn("taken by another session", out.getvalue())

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
        self.assertIn("--model <family>", out)
        self.assertNotIn("refresh the briefing", out)
        self.assertEqual(chat.bound_ids(self.r, self.prefix, sid), [named])
        self.assertFalse(self.r.cmd("HEXISTS", self.prefix + "agents", derived))
        self.assertIsNone(self.r.cmd("HGET", self.prefix + "cursor", derived))
        self.assertEqual(self.r.cmd("HGET", self.prefix + "agents", named), description)
        self.assertEqual(self.r.cmd("HGET", self.prefix + "cursor", named), "234-5")

    def test_legacy_followed_assigned_binding_is_marked_assigned_and_reserved(self):
        sid = "legacy-assigned-session-1234"
        named = self.prefix + "human-name"
        self.register(named, sid)
        self.r.cmd("HDEL", self.prefix + "session-kind", named)
        out = self.start_hook(sid)
        self.assertIn("you are agent " + named + ",", out)
        self.assertEqual(self.r.cmd("HGET", self.prefix + "session-kind", named), "assigned")
        chat.session_end(self.r, self.prefix, named, sid, {"reason": "exit"}, lambda _: None)
        result = subprocess.run(self.base + ["register", named, "--session", self.other, "new owner"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.owner(named), sid)

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

    def test_offline_keeps_binding_cursor_and_resume_reads_missed_messages(self):
        sid = "offline-session-1111"
        named = self.prefix + "assigned"
        self.register(named, sid)
        self.r.cmd("HSET", self.prefix + "cursor", named, "1-0")
        chat.session_end(self.r, self.prefix, self.agent, sid,
                         {"reason": "exit"}, lambda _: None)
        self.assertFalse(self.r.cmd("HEXISTS", self.prefix + "agents", named))
        self.assertEqual(self.r.cmd("HGET", self.prefix + "session", named), sid)
        self.assertEqual(self.r.cmd("HGET", self.prefix + "cursor", named), "1-0")
        self.assertTrue(self.r.cmd("HGET", self.prefix + "last-seen", named))
        self.assertEqual(self.main(self.r, "id-of", sid), 0)
        self.r.cmd("XADD", self.prefix + "chatroom", "*", "from", "other", "to", "all", "msg", "missed")
        self.start_hook(sid)
        out = subprocess.run(self.base + ["read", named], text=True, capture_output=True, timeout=10)
        self.assertIn("missed", out.stdout)

    def test_custom_binding_end_hook_follows_binding(self):
        sid = "custom-end-session-1234"
        named = self.prefix + "human-name"
        self.register(named, sid)
        chat.session_end(self.r, self.prefix, self.agent, sid,
                         {"reason": "exit"}, lambda _: None)
        self.assertFalse(self.r.cmd("HEXISTS", self.prefix + "agents", named))
        self.assertEqual(self.r.cmd("HGET", self.prefix + "session", named), sid)
        self.assertIsNone(self.r.cmd("HEXISTS", self.prefix + "agents", self.agent) or None)

    def test_session_deleted_is_gone_and_removes_all_lifecycle_state(self):
        sid = "gone-session-1234"
        named = self.prefix + "gone-name"
        self.register(named, sid)
        self.r.cmd("HSET", self.prefix + "cursor", named, "123-4")
        chat.session_end(self.r, self.prefix, named, sid,
                         {"reason": "session.deleted"}, lambda _: None)
        for key in ("agents", "cursor", "session", "session-kind", "last-seen", "model"):
            self.assertFalse(self.r.cmd("HEXISTS", self.prefix + key, named), key)

    def test_offline_assigned_name_reserved_force_reassigns(self):
        sid = "assigned-session-1234"
        named = self.prefix + "human-name"
        self.register(named, sid)
        chat.session_end(self.r, self.prefix, named, sid, {"reason": "exit"}, lambda _: None)
        result = subprocess.run(self.base + ["register", named, "--session", self.other, "other"],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn("taken:", result.stderr)
        self.register(named, self.other, force=True)
        self.assertEqual(self.owner(named), self.other)

    def test_offline_derived_collision_is_taken_over_without_old_cursor(self):
        old = "deadbeef-old-session"
        new = "deadbeef-new-session"
        derived = self.prefix + chat.session_tag(old)
        self.start_hook(old)
        self.r.cmd("HSET", self.prefix + "cursor", derived, "999-0")
        chat.session_end(self.r, self.prefix, derived, old, {"reason": "exit"}, lambda _: None)
        self.r.cmd("HSET", self.prefix + "cursor", derived, "999-0")
        result = subprocess.run(self.base + ["register", derived, "--session", new, "plain register"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.owner(derived), old)
        self.assertEqual(self.r.cmd("HGET", self.prefix + "cursor", derived), "999-0")
        # Different session id but same four-character derived tag.
        self.start_hook(new)
        self.assertEqual(self.owner(derived), new)
        self.assertFalse(self.r.cmd("HGET", self.prefix + "last-seen", derived))
        self.assertNotEqual(self.r.cmd("HGET", self.prefix + "cursor", derived), "999-0")
        self.assertEqual(self.r.cmd("HGET", self.prefix + "session-kind", derived), "derived")

    def test_forced_reassignment_turns_derived_id_into_reserved_assigned_name(self):
        old = "forcecoll-old-session"
        assigned_session = "forcecoll-assigned-session"
        other = "forcecoll-other-session"
        derived = self.prefix + chat.session_tag(old)
        self.start_hook(old)
        chat.session_end(self.r, self.prefix, derived, old, {"reason": "exit"}, lambda _: None)
        self.register(derived, assigned_session, force=True)
        self.assertEqual(self.r.cmd("HGET", self.prefix + "session-kind", derived), "assigned")
        chat.session_end(self.r, self.prefix, derived, assigned_session,
                         {"reason": "exit"}, lambda _: None)
        result = subprocess.run(self.base + ["register", derived, "--session", other, "attempt"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.owner(derived), assigned_session)

    def test_unregister_offline_reports_removed_binding(self):
        sid = "offline-unregister-1234"
        named = self.prefix + "offline"
        self.register(named, sid)
        chat.session_end(self.r, self.prefix, named, sid, {"reason": "exit"}, lambda _: None)
        result = subprocess.run(self.base + ["unregister", named, "--session", sid],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("was offline; binding and cursor removed", result.stderr)

    def test_prune_removes_expired_offline_only_and_force_unregister_is_gone(self):
        sid = "prune-session-1234"
        named = self.prefix + "assigned"
        self.register(named, sid)
        chat.session_end(self.r, self.prefix, named, sid, {"reason": "exit"}, lambda _: None)
        self.r.cmd("HSET", self.prefix + "last-seen", named, int(__import__('time').time()) - 1000)
        online = self.prefix + "online"
        self.register(online, "online-session-1234")
        removed = chat.prune_offline(self.r, self.prefix, 100)
        self.assertEqual(removed, [named])
        for key in ("session", "cursor", "session-kind", "last-seen"):
            self.assertFalse(self.r.cmd("HEXISTS", self.prefix + key, named))
        self.assertTrue(self.r.cmd("HEXISTS", self.prefix + "agents", online))

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

    def test_model_field_set_display_offline_resume_and_gone(self):
        sid = "model-session-1234"
        named = self.prefix + "named"
        self.register(named, sid, model="deepseek")
        self.assertEqual(self.r.cmd("HGET", self.prefix + "model", named), "deepseek")
        out = subprocess.run(self.base + ["agents"], capture_output=True, text=True, timeout=10)
        self.assertIn(f"{named} [deepseek]:", out.stdout)
        result = subprocess.run(self.base + ["model", named, "gpt"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.r.cmd("HGET", self.prefix + "model", named), "gpt")
        result = subprocess.run(self.base + ["model", named],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.stdout.strip(), "gpt")
        chat.session_end(self.r, self.prefix, named, sid, {"reason": "exit"}, lambda _: None)
        self.assertFalse(self.r.cmd("HEXISTS", self.prefix + "agents", named))
        self.assertEqual(self.r.cmd("HGET", self.prefix + "model", named), "gpt")
        out = self.start_hook(sid, model="gpt")
        self.assertIn(f"you are agent {named} [gpt],", out)
        self.assertEqual(self.r.cmd("HGET", self.prefix + "model", named), "gpt")
        chat.session_end(self.r, self.prefix, named, sid,
                         {"reason": "session.deleted"}, lambda _: None)
        self.assertFalse(self.r.cmd("HEXISTS", self.prefix + "model", named))

    def test_force_rename_moves_model_field(self):
        sid = "model-force-1234"
        first, second = self.prefix + "old", self.prefix + "new"
        self.register(first, sid, model="deepseek")
        result = subprocess.run(self.base + ["register", second, "--session", sid, "--force", "renamed"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.r.cmd("HGET", self.prefix + "model", second), "deepseek")
        self.assertFalse(self.r.cmd("HEXISTS", self.prefix + "model", first))

    def test_force_takeover_does_not_inherit_previous_owners_model(self):
        first, second, third = "model-takeover-first", "model-takeover-second", "model-takeover-third"
        name = self.prefix + "shared"
        self.register(name, first, model="gpt")
        result = subprocess.run(self.base + ["register", name, "--session", second, "--force", "B now"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.owner(name), second)
        self.assertIsNone(self.r.cmd("HGET", self.prefix + "model", name))
        # A new owner with its own model uses that model, not the old one.
        self.register(self.prefix + "own", third, model="deepseek")
        result = subprocess.run(self.base + ["register", name, "--session", third, "--force", "C now"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.owner(name), third)
        self.assertEqual(self.r.cmd("HGET", self.prefix + "model", name), "deepseek")

    def test_prune_removes_model_field(self):
        sid = "model-prune-1234"
        named = self.prefix + "pruned"
        self.register(named, sid, model="deepseek")
        chat.session_end(self.r, self.prefix, named, sid, {"reason": "exit"}, lambda _: None)
        self.r.cmd("HSET", self.prefix + "last-seen", named, int(__import__("time").time()) - 1000)
        self.assertEqual(chat.prune_offline(self.r, self.prefix, 100), [named])
        self.assertFalse(self.r.cmd("HEXISTS", self.prefix + "model", named))

    def test_derived_collision_takeover_clears_model_field(self):
        old, new = "modelcoll-old-session", "modelcoll-new-session"
        derived = self.prefix + chat.session_tag(old)
        self.start_hook(old, model="deepseek")
        self.assertEqual(self.r.cmd("HGET", self.prefix + "model", derived), "deepseek")
        chat.session_end(self.r, self.prefix, derived, old, {"reason": "exit"}, lambda _: None)
        self.start_hook(new, model="gpt")
        self.assertEqual(self.owner(derived), new)
        self.assertEqual(self.r.cmd("HGET", self.prefix + "model", derived), "gpt")

    def test_model_command_validation_and_manual_register(self):
        named = self.prefix + "manual"
        result = subprocess.run(self.base + ["register", named, "--model", "deepseek", "manual"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.r.cmd("HGET", self.prefix + "model", named), "deepseek")
        result = subprocess.run(self.base + ["model", self.prefix + "missing", "gpt"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        result = subprocess.run(self.base + ["model", named, "bad label"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        result = subprocess.run(self.base + ["unregister", named],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.r.cmd("HEXISTS", self.prefix + "model", named))


if __name__ == "__main__":
    unittest.main()
