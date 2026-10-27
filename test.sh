#!/bin/sh
# Self-test against a running Redis (CHATROOM_DB, default 7). Uses a prefix
# of its own and deletes only its own keys at the end (with redis-cli, which
# only the test needs).
set -e
cd "$(dirname "$0")"
export CHATROOM_PREFIX="chattest-$$-"
# the shared status key and the room scan are confined to this test's keys
export CHATROOM_STATUS_KEY="chattest-$$-status" CHATROOM_ROOMS="chattest-$$-*"
db=${CHATROOM_DB:-7}
C="python3 ./chat.py"
R="redis-cli -n $db"
# wait for the background writers first: on a failure they would otherwise
# post after the cleanup and leave a room behind
cleanup() { wait; $R --raw KEYS "${CHATROOM_PREFIX}*" | xargs -r $R DEL >/dev/null; }
trap cleanup EXIT
fail() { echo "FAIL: $*"; exit 1; }
# status <expected> <command...>: the command must exit with that status and
# never crash; its output is left in $out
status() {
  want=$1; shift; rc=0; out=$("$@" 2>&1) || rc=$?
  [ "$rc" = "$want" ] || fail "'$*' exited $rc, want $want: $out"
  echo "$out" | grep -q Traceback && fail "'$*' crashed: $out"
  return 0
}

$C register a "testing as a" ; $C register b "testing as b"
$C agents | grep -q "^a: testing as a" || fail agents
$C say a all "msg-for-everyone" >/dev/null
$C say a b "msg-for-b" >/dev/null
$C say b a "msg-for-a" >/dev/null
out=$($C read b); echo "$out" | grep -q "msg-for-everyone" && echo "$out" | grep -q "msg-for-b" || fail "read b: $out"
echo "$out" | grep -q "msg-for-a" && fail "b read a message to a"
echo "$out" | grep -q "msg-for-b" && echo "$out" | grep -q -- "a -> b" || fail "read b: sender/recipient missing: $out"
# a message with a line break and quotes comes back as sent
$C say a b "line one
line two \"quoted\"" >/dev/null
out=$($C read b); echo "$out" | grep -q "^line two \"quoted\"" || fail "multi-line message: $out"
[ -z "$($C read b)" ] || fail "cursor not moved"
# a newly registered agent starts at the end of the room; registering again
# keeps the cursor
$C register d "registered late"
[ -z "$($C read d)" ] || fail "new agent d saw old messages"
$C read d --all | grep -q "msg-for-everyone" || fail "read --all hides history"
$C register b "testing as b, again"
[ -z "$($C read b)" ] || fail "re-register moved b's cursor"
# unregister drops the agent and its cursor, lists what it still holds
$C register u "leaving soon"; $C read u >/dev/null
$C claim u held-topic; $C lock u held-lock
out=$($C unregister u 2>&1); echo "$out" | grep -q "still holds: claim held-topic, lock held-lock" || fail "unregister: $out"
$C agents | grep -q "^u:" && fail "unregister left u in agents"
[ -z "$($R HGET "${CHATROOM_PREFIX}cursor" u)" ] || fail "unregister left u's cursor"
$C claims | grep -q "^held-topic: u" || fail "unregister removed a claim"
$C unclaim u held-topic; $C unlock u held-lock
out=$($C unregister u 2>&1); echo "$out" | grep -q "wasn't registered" || fail "unregister of unknown id: $out"
status 2 $C unregister
status 2 $C unregister all
# hook-session-end: unregisters only the agent that registered with the same
# --session (id = prefix + first 4 chars of the session id); never fails;
# nothing on stdout
P=$CHATROOM_PREFIX; H="$C hook-session-end"
$C register "${P}s1aa" --session "s1aaaaaa-1111-2222-3333-444444444444" "with session"
$C register "${P}s2bb" "without session"
$C register "${P}s3aa" --session "s3aaaaaa-1111-2222-3333-444444444444" "other session"
hook() { out=$(printf '%s' "$1" | $H 2>"$errf"); rc=$?; err=$(cat "$errf")
  [ "$rc" = 0 ] || fail "hook exited $rc: $err"; [ -z "$out" ] || fail "hook wrote to stdout: $out"; }
errf=$(mktemp); trap 'cleanup; rm -f "$errf"' EXIT
hook '{"session_id":"s1aaaaaa-1111-2222-3333-444444444444","reason":"exit"}'
echo "$err" | grep -q "unregistered ${P}s1aa (exit)" || fail "hook s1: $err"
$C agents | grep -q "^${P}s1aa:" && fail "hook left s1aa in agents"
[ -z "$($R HGET "${P}session" "${P}s1aa")" ] || fail "hook left s1aa's session"
hook '{"session_id":"s2bbbbbb-0000"}'
echo "$err" | grep -q "didn't register with --session" || fail "hook s2: $err"
$C agents | grep -q "^${P}s2bb:" || fail "hook removed s2bb, which had no --session"
hook '{"session_id":"s3aabbbb-9999-8888-7777-666666666666"}'
echo "$err" | grep -q "belongs to session s3aaaaaa-1111-2222-3333-444444444444, not s3aabbbb-9999-8888-7777-666666666666" || fail "hook s3: $err"
$C agents | grep -q "^${P}s3aa:" || fail "hook removed s3aa on a 4-char collision"
hook '{"reason":"clear"}'; echo "$err" | grep -q "no session_id" || fail "hook without session_id: $err"
hook 'not json'; echo "$err" | grep -q "isn't the hook's JSON" || fail "hook with bad json: $err"
out=$(printf '{"session_id":"s1aaaaaa"}' | CHATROOM_PREFIX= $H 2>&1) || fail "hook without prefix exited $?"
echo "$out" | grep -q "no CHATROOM_PREFIX" || fail "hook without prefix: $out"
out=$(printf '{"session_id":"s1aaaaaa"}' | timeout -s KILL 10 $C --port 1 hook-session-end 2>&1) || fail "hook with Redis down exited $?"
echo "$out" | grep -q "error:" || fail "hook with Redis down: $out"
$C unregister "${P}s2bb"; $C unregister "${P}s3aa"
# hook-session-start registers prefix+4 chars with the session and prints a
# briefing on stdout; a second run keeps description and cursor; a colliding
# id of another session isn't touched; failures stay off stdout
HS="$C hook-session-start"
$C say "${P}s2bb" all "before-start" >/dev/null
out=$(printf '{"session_id":"n1nnnnnn-1111","source":"startup"}' | $HS 2>"$errf") || fail "start hook exited $?"
echo "$out" | grep -q "you are agent ${P}n1nn" || fail "start hook briefing: $out"
echo "$out" | grep -q "^  ${P}s2bb:" && fail "start hook listed unregistered s2bb" 
echo "$out" | grep -q "register ${P}n1nn --session n1nnnnnn-1111" || fail "start hook next steps: $out"
$C agents | grep -q "^${P}n1nn: new session (startup)" || fail "start hook didn't register: $($C agents)"
[ "$($R HGET "${P}session" "${P}n1nn")" = "n1nnnnnn-1111" ] || fail "start hook didn't store the session"
[ -z "$($C read "${P}n1nn")" ] || fail "start hook replayed history to the new agent"
$C register "${P}n1nn" --session n1nnnnnn-1111 "described now"
$C say "${P}s3aa" all "after-start" >/dev/null
out=$(printf '{"session_id":"n1nnnnnn-1111","source":"compact"}' | $HS 2>"$errf") || fail "start hook (compact) exited $?"
echo "$out" | grep -q "already registered" || fail "start hook (compact): $out"
$C agents | grep -q "^${P}n1nn: described now" || fail "start hook overwrote the description"
$C read "${P}n1nn" | grep -q after-start || fail "start hook (compact) moved the cursor"
out=$(printf '{"session_id":"n1nnzzzz-9999","source":"startup"}' | $HS 2>"$errf") || fail "start hook (collision) exited $?"
echo "$out" | grep -q "taken by another live session" || fail "start hook collision: $out"
grep -q "belongs to session n1nnnnnn-1111, not n1nnzzzz-9999" "$errf" || fail "start hook collision log lacks both full ids: $(cat "$errf")"
[ "$($R HGET "${P}session" "${P}n1nn")" = "n1nnnnnn-1111" ] || fail "start hook collision overwrote the session"
out=$(printf '{"session_id":"n1nnnnnn-1111"}' | timeout -s KILL 10 $C --port 1 hook-session-start 2>/dev/null) || fail "start hook with Redis down exited $?"
[ -z "$out" ] || fail "start hook with Redis down wrote to stdout: $out"
# Grok Build sends sessionId, not session_id, and does not put SessionStart
# stdout into the model's context. The briefing is saved and delivered once
# by hook-session-brief. Claude's PostToolUse of that command must stay quiet.
out=$(printf '{"sessionId":"k1kkkkkk-1111","source":"startup"}' | $HS 2>"$errf") || fail "grok start hook exited $?"
echo "$out" | grep -q "you are agent ${P}k1kk" || fail "grok start hook briefing: $out"
[ "$($R HGET "${P}session" "${P}k1kk")" = "k1kkkkkk-1111" ] || fail "grok start hook didn't store sessionId"
brief="$HOME/.cache/chatroom-briefs/k1kkkkkk-1111"
[ -f "$brief" ] || fail "grok start hook didn't save the briefing"
out=$(printf '{"sessionId":"k1kkkkkk-1111"}' | GROK_HOOK_EVENT=post_tool_use GROK_SESSION_ID=k1kkkkkk-1111 $C hook-session-brief 2>"$errf") || fail "brief hook exited $?"
echo "$out" | grep -q '"additionalContext"' || fail "brief hook didn't emit additionalContext: $out"
echo "$out" | grep -q "you are agent ${P}k1kk" || fail "brief hook dropped the text: $out"
[ ! -f "$brief" ] || fail "brief hook left the briefing file"
out=$(printf '{"sessionId":"k1kkkkkk-1111"}' | GROK_HOOK_EVENT=post_tool_use GROK_SESSION_ID=k1kkkkkk-1111 $C hook-session-brief 2>"$errf") || fail "brief hook second run exited $?"
[ -z "$out" ] || fail "brief hook spoke twice: $out"
printf 'should-stay\n' > "$brief"
out=$(printf '{"session_id":"k1kkkkkk-1111"}' | env -u GROK_SESSION_ID -u GROK_HOOK_EVENT $C hook-session-brief 2>"$errf") || fail "claude brief hook exited $?"
[ -z "$out" ] || fail "claude brief hook wrote stdout: $out"
[ "$(cat "$brief")" = "should-stay" ] || fail "claude brief hook ate the file"
rm -f "$brief"
hook '{"sessionId":"k1kkkkkk-1111","reason":"exit"}'
echo "$err" | grep -q "unregistered ${P}k1kk (exit)" || fail "grok end hook: $err"
$C agents | grep -q "^${P}k1kk:" && fail "grok end hook left k1kk"
# a session id only in GROK_SESSION_ID still registers the end hook's target
$C register "${P}k1kk" --session "k1kkkkkk-1111" "again"
out=$(printf '{}' | GROK_HOOK_EVENT=session_end GROK_SESSION_ID=k1kkkkkk-1111 $C hook-session-end 2>"$errf") || fail "grok end hook from env exited $?"
echo "$(cat "$errf")" | grep -q "unregistered ${P}k1kk" || fail "grok end hook from env: $(cat "$errf")"
[ -z "$out" ] || fail "grok end hook from env wrote stdout: $out"
# the briefing's commands name the host and port the hook used: the agent's
# shell doesn't have the hook's CHATROOM_HOST/CHATROOM_PORT
out=$(printf '{"session_id":"h0stxxxx-1111"}' | CHATROOM_HOST=localhost $HS 2>/dev/null)
echo "$out" | grep -q -- "--prefix $P --host localhost --port ${CHATROOM_PORT:-6379}\"" || fail "briefing lacks host/port: $(echo "$out" | grep 'with C=')"
printf '{"session_id":"h0stxxxx-1111","reason":"x"}' | $C hook-session-end 2>/dev/null
# and the end hook cleans up what the start hook made
hook '{"session_id":"n1nnnnnn-1111","reason":"exit"}'
$C agents | grep -q "^${P}n1nn:" && fail "end hook left n1nn"
# --id-prefix / CHATROOM_ID_PREFIX: the hooks' id gets that prefix instead of
# the key prefix; it must start with the key prefix and end in '-'
out=$(printf '{"session_id":"g1gggggg-1111","source":"startup"}' | CHATROOM_ID_PREFIX="${P}ge-" $HS 2>"$errf") || fail "start hook with id prefix exited $?"
echo "$out" | grep -q "you are agent ${P}ge-g1gg" || fail "start hook with id prefix: $out"
$C agents | grep -q "^${P}ge-g1gg: new session" || fail "id prefix not used: $($C agents)"
$C agents | grep -q "^${P}g1gg:" && fail "id prefix: also registered under the key prefix"
printf '{"session_id":"g1gggggg-1111","reason":"exit"}' | $C hook-session-end 2>/dev/null
$C agents | grep -q "^${P}ge-g1gg:" || fail "end hook without id prefix removed the ge- agent"
out=$(printf '{"session_id":"g1gggggg-1111","reason":"exit"}' | $C --id-prefix "${P}ge-" hook-session-end 2>&1) || fail "end hook with id prefix exited $?"
echo "$out" | grep -q "unregistered ${P}ge-g1gg" || fail "end hook with id prefix: $out"
$C agents | grep -q "^${P}ge-g1gg:" && fail "end hook with id prefix left the agent"
for bad in "other-ge-" "${P}ge" "${P}ge x-"; do
  out=$(printf '{"session_id":"g2gggggg-1111","source":"startup"}' | $C --id-prefix "$bad" hook-session-start 2>"$errf") || fail "start hook with id prefix '$bad' exited $?"
  [ -z "$out" ] || fail "start hook with bad id prefix '$bad' wrote to stdout: $out"
  grep -q "must start with the key prefix\|isn't a valid id" "$errf" || fail "start hook with bad id prefix '$bad': $(cat "$errf")"
done
$C agents | grep -q "g2gg" && fail "a bad id prefix registered something: $($C agents)"
# OpenCode session ids are "ses_" plus a time-ordered stem: sessions started
# the same day share the first characters after "ses_", so the tag is the
# last four (the random tail), not the first four.
ocid="ses_e8344a8b1ffeWx1qWX2mON5VQm"
ocid2="ses_e8349a629ffe3NlvDiYXyl5tnO"
out=$(printf '{"session_id":"%s","source":"startup"}' "$ocid" | CHATROOM_AGENT=opencode $HS 2>"$errf") || fail "opencode start hook exited $?"
echo "$out" | grep -q "you are agent ${P}5VQm" || fail "opencode tag: $out"
echo "$out" | grep -q "one OpenCode shell call with background: true" || fail "opencode watch hint: $out"
[ "$($R HGET "${P}session" "${P}5VQm")" = "$ocid" ] || fail "opencode start hook didn't store the session"
out=$(printf '{"session_id":"%s","source":"startup"}' "$ocid2" | CHATROOM_AGENT=opencode $HS 2>"$errf") || fail "second opencode start hook exited $?"
echo "$out" | grep -q "you are agent ${P}5tnO" || fail "second opencode tag: $out"
echo "$out" | grep -q "taken by another live session" && fail "two opencode sessions collided: $out"
printf '{"session_id":"%s","reason":"session.deleted"}' "$ocid" | CHATROOM_AGENT=opencode $C hook-session-end 2>"$errf"
grep -q "unregistered ${P}5VQm (session.deleted)" "$errf" || fail "opencode end hook: $(cat "$errf")"
printf '{"session_id":"%s","reason":"session.deleted"}' "$ocid2" | CHATROOM_AGENT=opencode $C hook-session-end 2>/dev/null
# --id / CHATROOM_ID: one fixed id for the hooks (a room's master, or an
# OpenCode agent the human named); it must start with the key prefix, and the
# end hook still only removes the session that registered it.
sida="ses_ocaaaaaa1111"; sidb="ses_ocbbbbbb2222"
out=$(printf '{"session_id":"%s","source":"startup"}' "$sida" | $C --id "${P}stable" hook-session-start 2>"$errf") || fail "fixed id start hook exited $?"
echo "$out" | grep -q "you are agent ${P}stable" || fail "fixed id briefing: $out"
$C agents | grep -q "^${P}stable: new session" || fail "fixed id not registered: $($C agents)"
printf '{"session_id":"%s","reason":"quit"}' "$sidb" | $C --id "${P}stable" hook-session-end 2>"$errf"
grep -q "belongs to session ${sida}, not ${sidb}" "$errf" || fail "fixed id end hook with another session: $(cat "$errf")"
$C agents | grep -q "^${P}stable:" || fail "fixed id end hook removed the wrong session"
printf '{"session_id":"%s","reason":"quit"}' "$sida" | $C --id "${P}stable" hook-session-end 2>"$errf"
grep -q "unregistered ${P}stable (quit)" "$errf" || fail "fixed id end hook: $(cat "$errf")"
$C agents | grep -q "^${P}stable:" && fail "fixed id end hook left the agent"
printf '{"session_id":"%s","source":"startup"}' "$sidb" | CHATROOM_ID="${P}envstable" $C hook-session-start 2>"$errf" || fail "CHATROOM_ID start hook exited $?"
$C agents | grep -q "^${P}envstable:" || fail "CHATROOM_ID not used: $($C agents)"
printf '{"session_id":"%s","reason":"x"}' "$sidb" | CHATROOM_ID="${P}envstable" $C hook-session-end 2>/dev/null
for bad in "other-stable" "bad id"; do
  out=$(printf '{"session_id":"%s"}' "$sida" | $C --id "$bad" hook-session-start 2>"$errf") || fail "fixed id '$bad' exited $?"
  [ -z "$out" ] || fail "fixed id '$bad' wrote to stdout: $out"
  grep -q "must start with the key prefix\|isn't a valid id" "$errf" || fail "fixed id '$bad': $(cat "$errf")"
done
$C agents | grep -q "stable" && fail "a bad fixed id registered something: $($C agents)"
# init writes both hooks into <dir>/.claude/settings.local.json, keeps other
# settings and other hooks, replaces its own on a second run, refuses bad input
idir=$(mktemp -d); trap 'cleanup; rm -f "$errf"; rm -rf "$idir"' EXIT
mkdir -p "$idir/.claude"
printf '{"permissions":{"allow":["Bash(ls)"]},"hooks":{"SessionEnd":[{"hooks":[{"type":"command","command":"echo bye"}]}]}}' > "$idir/.claude/settings.local.json"
$C init --dir "$idir" --id-prefix "${P}ge-" | grep -q "wrote the SessionStart and SessionEnd hooks" || fail "init output"
$C init --dir "$idir" --id-prefix "${P}ge-" >/dev/null
chk() { python3 - "$idir/.claude/settings.local.json" "$P" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1])); P = sys.argv[2]
assert cfg["permissions"]["allow"] == ["Bash(ls)"], "permissions lost"
cmds = {ev: [h["command"] for g in cfg["hooks"][ev] for h in g["hooks"]] for ev in ("SessionStart", "SessionEnd")}
assert any("echo bye" in c for c in cmds["SessionEnd"]), "foreign hook lost"
for ev, name in (("SessionStart", "hook-session-start"), ("SessionEnd", "hook-session-end")):
    ours = [c for c in cmds[ev] if name in c]
    assert len(ours) == 1, (ev, ours)
    assert f"CHATROOM_PREFIX={P} " in ours[0] and f"CHATROOM_ID_PREFIX={P}ge- " in ours[0] and "CHATROOM_DB=" in ours[0], ours[0]
    assert "python3 -I /" in ours[0] and 'chatroom-hooks.log' in ours[0], ours[0]
brief = [h["command"] for g in cfg["hooks"].get("PostToolUse", []) for h in g["hooks"] if "hook-session-brief" in h["command"]]
assert len(brief) == 1, brief
assert f"CHATROOM_PREFIX={P} " in brief[0] and f"CHATROOM_ID_PREFIX={P}ge- " in brief[0], brief[0]
print("ok")
PY
}
chk | grep -q ok || fail "init: settings content wrong"
# a repo in two rooms: init for the second room keeps the first room's hooks,
# init again for the first room replaces only its own
$C --prefix "${P}other-" init --dir "$idir" --id-prefix "${P}other-tag-" >/dev/null
$C init --dir "$idir" --id-prefix "${P}ge-" >/dev/null
python3 - "$idir/.claude/settings.local.json" "$P" <<'PY' | grep -q ok || fail "init: two rooms in one repo"
import json, sys
cfg = json.load(open(sys.argv[1])); P = sys.argv[2]
for ev in ("SessionStart", "SessionEnd"):
    cmds = [h["command"] for g in cfg["hooks"][ev] for h in g["hooks"] if "chat.py hook-session-" in h["command"]]
    rooms = sorted(c.split("CHATROOM_PREFIX=")[1].split(" ")[0] for c in cmds)
    assert rooms == sorted([P, P + "other-"]), (ev, rooms)
    assert any("echo bye" in h["command"] for g in cfg["hooks"]["SessionEnd"] for h in g["hooks"])
briefs = [h["command"] for g in cfg["hooks"]["PostToolUse"] for h in g["hooks"] if "hook-session-brief" in h["command"]]
rooms = sorted(c.split("CHATROOM_PREFIX=")[1].split(" ")[0] for c in briefs)
assert rooms == sorted([P, P + "other-"]), rooms
print("ok")
PY
$C --id "${P}fixed" init --dir "$idir" >/dev/null
python3 - "$idir/.claude/settings.local.json" "$P" <<'PY' | grep -q ok || fail "init --id: hook command"
import json, sys
cfg = json.load(open(sys.argv[1])); P = sys.argv[2]
for ev in ("SessionStart", "SessionEnd"):
    cmds = [h["command"] for g in cfg["hooks"][ev] for h in g["hooks"]
            if f"CHATROOM_PREFIX={P} " in h["command"]]
    assert len(cmds) == 1, cmds
    assert f"CHATROOM_ID={P}fixed " in cmds[0], cmds[0]
    assert "CHATROOM_ID_PREFIX=" not in cmds[0], cmds[0]
print("ok")
PY
status 2 $C init --dir "$idir" --id-prefix other-
status 2 $C init --dir "$idir" extra-arg
printf 'not json' > "$idir/.claude/settings.local.json"
status 2 $C init --dir "$idir"
[ "$(cat "$idir/.claude/settings.local.json")" = "not json" ] || fail "init overwrote a broken settings file"
rm -rf "$idir/.claude"; $C init --dir "$idir" >/dev/null && [ -f "$idir/.claude/settings.local.json" ] || fail "init without an existing file"
# init --opencode writes an OpenCode plugin instead of Claude hooks, with the
# room's config baked in; a second run replaces it, another room's plugin is
# kept, and the command it generated really registers a session
odir=$(mktemp -d); trap 'cleanup; rm -f "$errf"; rm -rf "$idir" "$odir"' EXIT
$C --id "${P}oc.stable" init --dir "$odir" --opencode | grep -q "wrote the OpenCode plugin" || fail "init --opencode output"
plug=$(find "$odir/.opencode/plugins" -name '*.js' | head -1)
[ -n "$plug" ] && [ -f "$plug" ] || fail "init --opencode wrote no plugin"
[ ! -e "$odir/.claude/settings.local.json" ] || fail "init --opencode also wrote Claude hooks"
if command -v node >/dev/null 2>&1; then
  node --check "$plug" || fail "generated OpenCode plugin isn't valid JavaScript"
  PLUG="$plug" node -e '
    const fs = require("fs")
    const text = fs.readFileSync(process.env.PLUG, "utf8")
    const found = /const mine = (\/.*?\/)\.exec\(text\)/.exec(text)
    if (!found) throw new Error("no agent id regex in the plugin")
    const re = eval(found[1])
    const got = re.exec("Chatroom x- (chat.py): you are agent x-oc.stable, registered with your session id")
    if (!got || got[1] !== "x-oc.stable") throw new Error("parsed " + JSON.stringify(got && got[1]))
  ' || fail "generated plugin's id regex drops a dotted id"
fi
python3 - "$plug" "$P" "$db" <<'PY' | grep -q ok || fail "init --opencode: plugin content or its command"
import json, os, re, subprocess, sys
path, P, db = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path).read()
cfg = json.loads(re.search(r"const CONFIG = (\{.*?\n\})", text, re.S).group(1))
assert cfg["prefix"] == P and cfg["id"] == P + "oc.stable", cfg
assert cfg["idPrefix"] == P and cfg["db"] == int(db), cfg
assert cfg["chat"].endswith("chat.py") and cfg["python"], cfg
assert "__CONFIG__" not in text and "__ID__" not in text and "__ROOM__" not in text, "placeholder left"
assert "Plugin.define" in text and 'ctx.session.hook("context"' in text, "not an OpenCode plugin"
assert "hook-session-start" in text and "hook-session-end" in text, "hooks missing"
assert r"/you are agent (\S+?),/" in text, "id regex must stop at the comma"
assert '"--host", CONFIG.host, "--port", String(CONFIG.port)' in text, "BASE lacks host/port"
assert 'const CMD = [CONFIG.python, ...BASE].map(shquote).join(" ")' in text, "CMD not built from ARGS"
assert "deadline = Date.now() + 5000" in text, "no shutdown time budget"
assert "sameProject" in text and '"skip " + sessionID' in text, "project filter is not fail-closed"
assert "session.deleted for live" in text, "no liveness check for spurious session.deleted"
sid = "ses_ocinit1111abcd"
env = dict(os.environ, CHATROOM_AGENT="opencode")
p = subprocess.run([cfg["python"], "-I", cfg["chat"], "--db", str(cfg["db"]),
                    "--prefix", cfg["prefix"], "--id", cfg["id"], "hook-session-start"],
                   input=json.dumps({"session_id": sid, "source": "opencode"}),
                   text=True, capture_output=True, env=env)
assert p.returncode == 0, (p.returncode, p.stderr)
assert ("you are agent " + cfg["id"]) in p.stdout, p.stdout
print("ok")
PY
printf '{"session_id":"ses_ocinit1111abcd","reason":"test"}' | $C --id "${P}oc.stable" hook-session-end 2>/dev/null
$C agents | grep -q "^${P}oc.stable:" && fail "init --opencode test left the agent"
$C --id "${P}oc.stable" init --dir "$odir" --opencode >/dev/null
[ "$(find "$odir/.opencode/plugins" -name '*.js' | wc -l)" = 1 ] || fail "init --opencode duplicated its plugin"
[ -z "$(find "$odir/.opencode/plugins" -name '*.new')" ] || fail "init --opencode left a temporary file"
$C --prefix "${P}two-" --id "${P}two-stable" init --dir "$odir" --opencode >/dev/null
[ "$(find "$odir/.opencode/plugins" -name '*.js' | wc -l)" = 2 ] || fail "init --opencode dropped another room's plugin"
status 2 $C --id bad init --dir "$odir" --opencode
[ "$(find "$odir/.opencode/plugins" -name '*.js' | wc -l)" = 2 ] || fail "a bad fixed id wrote a plugin"
status 2 $C init --dir "$odir" --opencode extra-arg
$C read b >/dev/null  # the messages above are not for the watch test below
# watch: wakes on a message to me, not on my own or others'
( sleep 1; $C say b all "from-b-itself" >/dev/null; $C say c a "for-a-not-b" >/dev/null; $C say a b "wake up" >/dev/null ) &
out=$(timeout -s KILL 10 $C watch b) || fail "watch timed out"
echo "$out" | grep -q "wake up" || fail "watch: $out"
echo "$out" | grep -q "from-b-itself\|for-a-not-b" && fail "watch woke for the wrong message: $out"
wait
$C read b >/dev/null
# watch --timeout ends with 124 and a note; an agent without a cursor waits
# for new messages only
status 124 timeout -s KILL 10 $C watch b --timeout 1
echo "$out" | grep -q "no message" || fail "watch --timeout: $out"
( sleep 1; $C say a all "late-hello" >/dev/null ) &
out=$(timeout -s KILL 10 $C watch e --timeout 5) || fail "watch e: $out"
echo "$out" | grep -q "late-hello" || fail "watch e: $out"
echo "$out" | grep -q "msg-for-everyone" && fail "watch without cursor replayed history: $out"
wait
# recipient lists: "a,b" reaches both, "all,!b" everyone but b
$C read a >/dev/null; $C read b >/dev/null; $C read d >/dev/null
$C say c "a,b" "for-a-and-b" >/dev/null
$C say c "all,!b" "not-for-b" >/dev/null
$C say c "all, !a ,d" "not-for-a" >/dev/null
out=$($C read a); echo "$out" | grep -q for-a-and-b && echo "$out" | grep -q not-for-b || fail "list read a: $out"
echo "$out" | grep -q not-for-a && fail "a got a message excluding a: $out"
out=$($C read b); echo "$out" | grep -q for-a-and-b && echo "$out" | grep -q not-for-a || fail "list read b: $out"
echo "$out" | grep -q not-for-b && fail "b got a message excluding b: $out"
out=$($C read d); echo "$out" | grep -q not-for-b && echo "$out" | grep -q not-for-a || fail "list read d: $out"
echo "$out" | grep -q for-a-and-b && fail "d got a message for a,b: $out"
( sleep 1; $C say c "all,!b" "skip-b" >/dev/null; $C say c "all,!a" "wake-b" >/dev/null ) &
out=$(timeout -s KILL 10 $C watch b) || fail "watch b (list) timed out"
echo "$out" | grep -q wake-b || fail "watch b (list): $out"
echo "$out" | grep -q skip-b && fail "watch b woke on an excluding message: $out"
wait; $C read a >/dev/null; $C read b >/dev/null
# broadcast reaches all in every room of the scan; status set/clear broadcast
# and gate claim/lock
P2="chattest-$$-two-"
$C --prefix "$P2" register z "in the second room"
$C broadcast a "to-every-room" | grep -q "2 room(s)" || fail "broadcast room count: $($C broadcast a x)"
$C --prefix "$P2" read z | grep -q to-every-room || fail "broadcast missed the second room"
$C read b 2>/dev/null | grep -q to-every-room || fail "broadcast missed the first room"
$C status && [ -z "$($C status)" ] || fail "status should be empty"
status 2 $C status set a nonsense
status 2 $C status set a warn --ttl x
$C status set a warn "90% used" | grep -q "told all in 2" || fail "status set warn"
$C status >/dev/null && fail "status exit 0 while set"
$C status | grep -q "^status: warn a 90% used" || fail "status show: $($C status)"
$C --prefix "$P2" read z 2>/dev/null | grep -q "STATUS WARN.*90% used" || fail "status broadcast: $($C --prefix "$P2" read z --all | tail -2)"
out=$($C claim a warned-topic 2>&1) || fail "claim refused under warn: $out"
echo "$out" | grep -q "start nothing new" || fail "claim under warn silent: $out"
$C status set a pause --ttl 60 "limit in 10 min" >/dev/null
status 1 $C claim a paused-topic
echo "$out" | grep -q "refused, shared status is pause" || fail "claim under pause: $out"
status 1 $C lock a paused-lock
$C status set a pause-sub-agents "hard limit soon" >/dev/null
status 1 $C claim a paused-topic
$C --prefix "$P2" read z 2>/dev/null | grep -q "STATUS PAUSE-SUB-AGENTS: pause your running sub-agents now" || fail "pause-sub-agents broadcast"
$C status set a pause --ttl 60 "limit in 10 min" >/dev/null
$C unclaim a warned-topic
$C read a 2>&1 >/dev/null | grep -q "status: pause a limit in 10 min.*expires in" || fail "read without status line: $($C read a 2>&1)"
$C status clear a | grep -q "cleared, told all in 2" || fail "status clear"
$C status || fail "status not cleared"
$C --prefix "$P2" read z | grep -q "STATUS RESUMED" || fail "resume broadcast"
$C claim a paused-topic || fail "claim after clear"; $C unclaim a paused-topic
$C status clear a 2>&1 | grep -q "no status was set" || fail "double clear"
$C read a >/dev/null; $C read b >/dev/null
# usage errors: exit 2 with a message, never a traceback
status 2 $C lock a s --ttl
status 2 $C lock a s --ttl abc
status 2 $C claim a s --ttl 0
status 2 $C register all "nobody"
status 2 $C register "a b" "space"
status 2 $C say a "x y" hi
status 2 $C say a ALL hi
status 2 $C say a "!b" hi
status 2 $C say a "all,,x y" hi
status 2 $C claim a "bad topic"
status 2 $C say a all
# claims and locks
$C claim a area9 "fixing it"
$C claim b area9 >/dev/null 2>&1 && fail "second claim worked"
$C unclaim b area9 >/dev/null 2>&1 && fail "unclaim of someone else's claim worked"
$C claims | grep -q "^area9: a fixing it" || fail claims
$C unclaim a area9; $C claim b area9
$C claim a "area9:Obj/Lock.throw-int" "topic with : / ." || fail "topic with : / . rejected"
$C claims | grep -q "^area9:Obj/Lock.throw-int: a topic" || fail "claims with : / .: $($C claims)"
$C unclaim a nothing-here 2>/dev/null || fail "unclaim of a missing claim failed"
$C lock a pr-slot --ttl 60 "PR 1"
$C lock b pr-slot >/dev/null 2>&1 && fail "second lock worked"
$C unlock a pr-slot; $C lock b pr-slot
# a claim with --ttl expires and shows the time left
$C claim a t1 --ttl 1 "short"
$C claim b t1 >/dev/null 2>&1 && fail "claim of a live ttl claim worked"
$C claims | grep -q "^t1: a short.*expires in" || fail "claims without time left: $($C claims)"
sleep 1.5
$C claim b t1 || fail "ttl claim did not expire"
# read warns when the room trimmed messages posted since the last read
$C read b >/dev/null
for i in 1 2 3; do $C say a all "flood-$i" >/dev/null; done
$R XTRIM "${CHATROOM_PREFIX}chatroom" MAXLEN 2 >/dev/null
out=$($C read b 2>&1); echo "$out" | grep -q "trimmed" || fail "no trim warning: $out"
echo "$out" | grep -q "flood-3" || fail "read after trim lost the remaining messages: $out"
out=$($C read b 2>&1); [ -z "$out" ] || fail "trim warning repeated: $out"
echo "all tests passed"
