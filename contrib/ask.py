#!/usr/bin/env python3
"""Send an ASK to one agent in a room and wait for its ACK / NACK.

    CHATROOM_PREFIX=<room>- python3 -I contrib/ask.py <from> <to> <tag> "<request>" [--wait SECONDS]

A reply convention on top of chat.py, so that a request to an agent whose
watcher has died is noticed instead of waiting forever:

    ASK <tag>: <request>        sender -> recipient
    ACK <tag> [eta / note]      recipient: got it, working on it
    NACK <tag> <reason>         recipient: won't or can't. When it is blocked on
                                its human: "NACK <tag> waiting-for-<human>: <question>"
    DONE <tag> <summary>        recipient: finished
    PING <tag>                  sender: no ACK/NACK after --wait seconds (sent once)

No answer after the ASK and the one PING means the recipient is most likely
idle with no watcher running, and only its human can wake it.

Run it as a tracked background task of the harness, like `watch`. It prints the
recipient's state before the ASK, then the reply, and exits:

    0    ACK, NACK or DONE received from <to> with this tag (printed)
    2    usage (no room prefix, wrong arguments)
    3    chat.py or redis-cli failed
    124  nothing after the ASK and one PING (default 300 s each), as `watch --timeout`

The recipient's state ([watching], [not watching, 2 unread]) comes from
`chat.py agents`, when that chat.py tracks watchers; otherwise it reads
"state unknown". The room is read with `redis-cli XRANGE`, which moves nobody's cursor, so the
sender's own `read` still shows the reply. CHATROOM_DB (7), CHATROOM_HOST,
CHATROOM_PORT and CHATROOM_PASSWORD are honoured as by chat.py.
"""
import json
import os
import subprocess
import sys
import time

CHAT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "chat.py")
DB = os.environ.get("CHATROOM_DB", "7")
PREFIX = os.environ.get("CHATROOM_PREFIX", "")
POLL = 10


def chat(*args):
    r = subprocess.run(["python3", "-I", CHAT, "--db", DB, "--prefix", PREFIX, *args],
                       capture_output=True, text=True)
    if r.returncode not in (0, 1):
        print(f"chat.py {args[0]} failed: {r.stderr.strip()}", file=sys.stderr)
        sys.exit(3)
    return r.stdout.strip()


def redis_cli(*args):
    cmd = ["redis-cli", "-n", DB, "--json"]
    if os.environ.get("CHATROOM_HOST"):
        cmd += ["-h", os.environ["CHATROOM_HOST"]]
    if os.environ.get("CHATROOM_PORT"):
        cmd += ["-p", os.environ["CHATROOM_PORT"]]
    env = dict(os.environ)
    if os.environ.get("CHATROOM_PASSWORD"):
        env["REDISCLI_AUTH"] = os.environ["CHATROOM_PASSWORD"]  # not on the command line
    r = subprocess.run(cmd + list(args), capture_output=True, text=True, env=env)
    if r.returncode != 0:
        print(f"redis-cli failed: {r.stderr.strip()}", file=sys.stderr)
        sys.exit(3)
    return json.loads(r.stdout or "null")


def entries_after(start):
    return [(eid, dict(zip(f[0::2], f[1::2])))
            for eid, f in redis_cli("XRANGE", PREFIX + "chatroom", "(" + start, "+") or []]


def state(to):
    """The recipient's bracketed state from `agents` ([watching], [not watching, 1 unread], ...)."""
    for line in chat("agents").splitlines():
        if line.startswith((to + " ", to + ":")):
            # Only a chat.py that tracks watchers ends the line with [watching] / [not watching, ...].
            return line.rsplit("[", 1)[-1].rstrip("]") if line.endswith("]") else "state unknown"
    return "not registered"


def wait_reply(to, tag, start, seconds):
    end = time.time() + seconds
    while time.time() < end:
        for eid, f in entries_after(start):
            start = eid
            word = f.get("msg", "").split(None, 2)
            if (f.get("from") == to and len(word) >= 2 and word[0] in ("ACK", "NACK", "DONE")
                    and word[1].rstrip(":") == tag):
                return f"{f.get('time')} {to}: {f.get('msg')}", start
        time.sleep(POLL)
    return None, start


def main():
    args = sys.argv[1:]
    wait = 300
    if "--wait" in args:
        i = args.index("--wait")
        wait = int(args[i + 1])
        del args[i:i + 2]
    if len(args) != 4 or not PREFIX.endswith("-"):
        print(__doc__, file=sys.stderr)
        return 2
    me, to, tag, request = args
    print(f"{to} is [{state(to)}] before the ASK")
    start = chat("say", me, to, f"ASK {tag}: {request} "
                 f"(reply ACK {tag} / NACK {tag} <reason>; DONE {tag} <summary> when finished)")
    reply, start = wait_reply(to, tag, start, wait)
    if reply:
        print(reply)
        return 0
    print(f"no reply after {wait}s; {to} is [{state(to)}]; sending one PING")
    start = chat("say", me, to, f"PING {tag}: no ACK/NACK yet for ASK {tag}. Please reply, and "
                 f"restart your watcher (chat.py watch {to} --timeout 3600, as a tracked background task).")
    reply, start = wait_reply(to, tag, start, wait)
    if reply:
        print(reply)
        return 0
    print(f"NO RESPONSE from {to} after ASK + PING ({2 * wait}s); it is [{state(to)}]. "
          f"Its watcher is probably dead: ask its human to nudge that session.")
    return 124


if __name__ == "__main__":
    sys.exit(main())
