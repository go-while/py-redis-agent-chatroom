#!/bin/sh
# Self-test against a running Redis (CHATROOM_DB, default 7). Uses a prefix
# of its own and deletes only its own keys at the end (with redis-cli, which
# only the test needs).
set -e
cd "$(dirname "$0")"
export CHATROOM_PREFIX="chattest-$$-"
db=${CHATROOM_DB:-7}
C="python3 ./chat.py"
R="redis-cli -n $db"
cleanup() { $R --raw KEYS "${CHATROOM_PREFIX}*" | xargs -r $R DEL >/dev/null; }
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
# usage errors: exit 2 with a message, never a traceback
status 2 $C lock a s --ttl
status 2 $C lock a s --ttl abc
status 2 $C claim a s --ttl 0
status 2 $C register all "nobody"
status 2 $C register "a b" "space"
status 2 $C say a "x y" hi
status 2 $C say a ALL hi
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
