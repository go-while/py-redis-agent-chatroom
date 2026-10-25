#!/usr/bin/env python3
"""A chatroom for AI agents that work in parallel, each in its own session,
on a local Redis: messages, a list of agents, claims and locks.

Every key starts with a project prefix (e.g. "nc-"), so several projects can
share one Redis database. Talks to Redis itself (RESP over a socket): no
redis-cli and no Python module needed. Needs Redis 6.2 or newer.

    chat.py [--db N] [--prefix P] [--host H] [--port N] <command> ...

    register <id> <what you work on>   enter yourself in <prefix>agents; a new
                                       id starts reading at the end of the room
    agents                             who works on what
    say <id> <to> <message>            post to the room; <to> = all or an id
    read <id> [--all]                  new messages for <id> since its cursor
                                       (--all: every message, from the start);
                                       moves the cursor; warns on stderr when
                                       the room trimmed messages you never read
    watch <id> [--timeout S]           wait until a message for <id> arrives
                                       from someone else, print it, exit 0 (run
                                       it in the background: the exit is the
                                       wake-up); doesn't move the cursor. With
                                       --timeout, give up after S seconds and
                                       exit 124
    claim <id> <topic> [--ttl S] [note] claim a topic (exit 1 if someone else
                                       has it, and print who); with --ttl the
                                       claim expires by itself
    unclaim <id> <topic>               give a claim up (only your own)
    claims                             all claims, with the time left if any
    lock <id> <name> [--ttl S] [note]  take a lock, e.g. "pr-slot" (exit 1 if
                                       taken, and print by whom)
    unlock <id> <name>                 release a lock (only your own)

Ids: 1-64 of letters, digits, ".", "_", "-"; never "all". Topics and lock
names: 1-128 of the same plus "/" and ":".
Exit status: 0 ok; 1 taken or not yours; 2 usage; 3 Redis unreachable or
failed; 124 watch --timeout ran out.

Defaults come from the environment: CHATROOM_DB (7), CHATROOM_PREFIX
(required, e.g. "nc-"), CHATROOM_HOST (127.0.0.1), CHATROOM_PORT (6379),
CHATROOM_PASSWORD (none), CHATROOM_MAXLEN (1000: about how many messages the
room keeps before it trims the oldest).
Keys: <prefix>chatroom (stream: from, to, time, msg), <prefix>agents (hash),
<prefix>cursor (hash: id -> last read entry), <prefix>claim:<topic>,
<prefix>lock:<name>.
"""
import argparse
import os
import re
import socket
import sys
import time

ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
TOPIC_RE = re.compile(r"^[A-Za-z0-9._/:-]{1,128}$")
MAXLEN = os.environ.get("CHATROOM_MAXLEN", "1000")

# Delete KEYS[1] only if its first word (the owner id) is ARGV[1]:
# 1 = released, 0 = nothing there, the value = someone else owns it.
RELEASE = """
local v = redis.call('GET', KEYS[1])
if not v then return 0 end
if string.match(v, '^(%S+)') ~= ARGV[1] then return v end
return redis.call('DEL', KEYS[1])
"""


class RedisError(Exception):
    pass


class Redis:
    """The few bits of RESP2 this needs: commands as arrays of bulk strings,
    and the five reply types. Every command has a 10 s timeout; watch raises
    it for its blocking reads."""

    def __init__(self, host, port, db, password=None):
        self.sock = socket.create_connection((host, port), timeout=10)
        self.buf = b""
        if password:
            self.cmd("AUTH", password)
        if db:
            self.cmd("SELECT", db)

    def cmd(self, *args):
        out = [b"*%d\r\n" % len(args)]
        for a in args:
            b = a if isinstance(a, bytes) else str(a).encode()
            out.append(b"$%d\r\n%s\r\n" % (len(b), b))
        self.sock.sendall(b"".join(out))
        return self._reply()

    def _line(self):
        while b"\r\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RedisError("connection closed by the server")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line

    def _exact(self, n):
        while len(self.buf) < n + 2:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RedisError("connection closed by the server")
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n + 2:]
        return data

    def _reply(self):
        line = self._line()
        kind, rest = line[:1], line[1:]
        if kind == b"+":
            return rest.decode()
        if kind == b"-":
            raise RedisError(rest.decode())
        if kind == b":":
            return int(rest)
        if kind == b"$":
            n = int(rest)
            return None if n < 0 else self._exact(n).decode("utf-8", "replace")
        if kind == b"*":
            n = int(rest)
            return None if n < 0 else [self._reply() for _ in range(n)]
        raise RedisError("unexpected reply %r" % line)


def die(msg, code=2):
    print(msg, file=sys.stderr)
    sys.exit(code)


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def check_id(s, what="id", allow_all=False):
    if s == "all" and allow_all:
        return s
    if s.lower() == "all":
        die(f"{what} {s!r}: 'all' means everyone" + ("; write it in lower case" if allow_all else ""))
    if not ID_RE.match(s):
        die(f"{what} {s!r}: use 1-64 of letters, digits, '.', '_', '-'")
    return s


def check_topic(s, what):
    if not TOPIC_RE.match(s):
        die(f"{what} {s!r}: use 1-128 of letters, digits, '.', '_', '-', '/', ':'")
    return s


def take_opt(args, name):
    """Remove '<name> N' from args. Returns (N or None, the rest)."""
    if name not in args:
        return None, args
    i = args.index(name)
    if i + 1 >= len(args) or not args[i + 1].isdigit() or int(args[i + 1]) < 1:
        die(f"{name} needs a whole number of seconds, 1 or more")
    return int(args[i + 1]), args[:i] + args[i + 2:]


def sid(s):
    """Stream id 'ms-seq' as something comparable."""
    ms, _, seq = s.partition("-")
    return int(ms), int(seq or 0)


def glob_escape(s):
    return re.sub(r"([*?\[\]\\])", r"\\\1", s)


def entries(reply):
    """XRANGE reply -> [(id, {field: value})]."""
    res = []
    for eid, flat in reply or []:
        res.append((eid, dict(zip(flat[0::2], flat[1::2]))))
    return res


def show(eid, f):
    print(f"{eid} {f.get('time', '')} {f.get('from')} -> {f.get('to')}: {f.get('msg')}")


def for_me(me, f):
    return f.get("from") != me and f.get("to") in ("all", me)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("--db", type=int, default=int(os.environ.get("CHATROOM_DB", "7")))
    ap.add_argument("--prefix", default=os.environ.get("CHATROOM_PREFIX"))
    ap.add_argument("--host", default=os.environ.get("CHATROOM_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("CHATROOM_PORT", "6379")))
    ap.add_argument("cmd")
    ap.add_argument("args", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    if not a.prefix or not a.prefix.endswith("-"):
        die("set a project prefix ending in '-' (--prefix nc- or CHATROOM_PREFIX)")
    if not MAXLEN.isdigit() or int(MAXLEN) < 1:
        die("CHATROOM_MAXLEN must be a whole number, 1 or more")
    try:
        r = Redis(a.host, a.port, a.db, os.environ.get("CHATROOM_PASSWORD"))
    except OSError as e:
        die(f"can't connect to Redis at {a.host}:{a.port}: {e}", 3)
    args, p = a.args, a.prefix
    stream, agents, cursor = p + "chatroom", p + "agents", p + "cursor"

    def need(n, usage):
        if len(args) < n:
            die("usage: chat.py " + usage)

    def last_id():
        tail = entries(r.cmd("XREVRANGE", stream, "+", "-", "COUNT", "1"))
        return tail[0][0] if tail else "0-0"

    if a.cmd == "register":
        need(2, "register <id> <what you work on>")
        me = check_id(args[0])
        r.cmd("HSET", agents, me, " ".join(args[1:]) + " | " + now())
        # a new agent starts at the end of the room; an existing cursor stays
        r.cmd("HSETNX", cursor, me, last_id())
    elif a.cmd == "agents":
        flat = r.cmd("HGETALL", agents) or []
        for k, v in sorted(zip(flat[0::2], flat[1::2])):
            print(f"{k}: {v}")
    elif a.cmd == "say":
        need(3, "say <id> <to> <message>")
        me, to = check_id(args[0]), check_id(args[1], "recipient", allow_all=True)
        print(r.cmd("XADD", stream, "MAXLEN", "~", MAXLEN, "*", "from", me,
                    "to", to, "time", now(), "msg", " ".join(args[2:])))
    elif a.cmd == "read":
        need(1, "read <id> [--all]")
        me, everything = check_id(args[0]), "--all" in args[1:]
        if everything:
            es = entries(r.cmd("XRANGE", stream, "-", "+"))
        else:
            last = r.cmd("HGET", cursor, me) or "0-0"
            es = entries(r.cmd("XRANGE", stream, "(" + last, "+"))
            if last != "0-0":
                first = entries(r.cmd("XRANGE", stream, "-", "+", "COUNT", "1"))
                if first and sid(first[0][0]) > sid(last):
                    print(f"warning: the room trimmed messages posted since your last read "
                          f"(it keeps about {MAXLEN}); some for you may be lost", file=sys.stderr)
        for eid, f in es:
            if everything or for_me(me, f):
                show(eid, f)
        if es:
            r.cmd("HSET", cursor, me, es[-1][0])
    elif a.cmd == "watch":
        timeout, args = take_opt(args, "--timeout")
        need(1, "watch <id> [--timeout S]")
        me = check_id(args[0])
        # without a cursor, wait for new messages only ("$"), not the history
        last = r.cmd("HGET", cursor, me) or "$"
        deadline = time.monotonic() + timeout if timeout else None
        while True:
            block = 600000
            if deadline:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                block = min(block, int(left * 1000) + 1)
            r.sock.settimeout(block / 1000 + 10)
            reply = r.cmd("XREAD", "BLOCK", block, "COUNT", "100", "STREAMS", stream, last)
            if not reply:
                continue  # Redis timed out: wait again
            es = entries(reply[0][1])
            last = es[-1][0]
            hits = [(eid, f) for eid, f in es if for_me(me, f)]
            if hits:
                for eid, f in hits:
                    show(eid, f)
                print(f"(then: chat.py read {me}, which also moves your cursor)")
                return
        die(f"(no message for {me} within {timeout} s)", 124)
    elif a.cmd in ("claim", "lock"):
        what = "topic" if a.cmd == "claim" else "name"
        ttl, args = take_opt(args, "--ttl")
        need(2, f"{a.cmd} <id> <{what}> [--ttl S] [note]")
        me, topic = check_id(args[0]), check_topic(args[1], what)
        k = f"{p}{a.cmd}:{topic}"
        val = me + (" " + " ".join(args[2:]) if len(args) > 2 else "") + " | " + now()
        if r.cmd("SET", k, val, "NX", *(["EX", ttl] if ttl else [])) != "OK":
            print(f"taken: {r.cmd('GET', k) or '(and released just now; try again)'}")
            sys.exit(1)
    elif a.cmd in ("unclaim", "unlock"):
        kind = "claim" if a.cmd == "unclaim" else "lock"
        what = "topic" if kind == "claim" else "name"
        need(2, f"{a.cmd} <id> <{what}>")
        me, topic = check_id(args[0]), check_topic(args[1], what)
        res = r.cmd("EVAL", RELEASE, "1", f"{p}{kind}:{topic}", me)
        if res == 0:
            print(f"nothing to release: {kind} {topic} isn't held", file=sys.stderr)
        elif res != 1:
            print(f"not yours: {res}")
            sys.exit(1)
    elif a.cmd == "claims":
        cur, keys = "0", []
        while True:
            cur, batch = r.cmd("SCAN", cur, "MATCH", glob_escape(p) + "claim:*", "COUNT", "1000")
            keys += batch
            if cur == "0":
                break
        for k in sorted(set(keys)):
            v = r.cmd("GET", k)
            if v is None:
                continue  # expired between SCAN and GET
            ttl = r.cmd("TTL", k)
            print(f"{k[len(p + 'claim:'):]}: {v}" + (f" (expires in {ttl}s)" if ttl >= 0 else ""))
    else:
        die(f"unknown command {a.cmd!r}; see chat.py --help")


if __name__ == "__main__":
    try:
        main()
    except (RedisError, OSError) as e:
        die(f"redis: {e}", 3)
