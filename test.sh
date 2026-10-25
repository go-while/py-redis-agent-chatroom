#!/bin/sh
# Self-test against a running Redis (CHATROOM_DB, default 7). Uses a prefix
# of its own and deletes only its own keys at the end (with redis-cli, which
# only the test needs).
set -e
cd "$(dirname "$0")"
export CHATROOM_PREFIX="chattest-$$-"
db=${CHATROOM_DB:-7}
C="python3 ./chat.py"
cleanup() { redis-cli -n "$db" --raw KEYS "${CHATROOM_PREFIX}*" | xargs -r redis-cli -n "$db" DEL >/dev/null; }
trap cleanup EXIT
fail() { echo "FAIL: $*"; exit 1; }

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
# watch: wakes on a message to me, not on my own or others'
( sleep 1; $C say b all "from-b-itself" >/dev/null; $C say c a "for-a-not-b" >/dev/null; $C say a b "wake up" >/dev/null ) &
out=$(timeout 10 $C watch b) || fail "watch timed out"
echo "$out" | grep -q "wake up" || fail "watch: $out"
echo "$out" | grep -q "from-b-itself\|for-a-not-b" && fail "watch woke for the wrong message: $out"
wait
# claims and locks
$C claim a area9 "fixing it"
$C claim b area9 >/dev/null 2>&1 && fail "second claim worked"
$C unclaim b area9 >/dev/null 2>&1 && fail "unclaim of someone else's claim worked"
$C claims | grep -q "^area9: a fixing it" || fail claims
$C unclaim a area9; $C claim b area9
$C lock a pr-slot --ttl 60 "PR 1"
$C lock b pr-slot >/dev/null 2>&1 && fail "second lock worked"
$C unlock a pr-slot; $C lock b pr-slot
echo "all tests passed"
