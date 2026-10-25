#!/usr/bin/env python3
"""A chatroom for AI agents that work in parallel, each in its own session,
on a local Redis: messages, a list of agents, claims and locks.

Every key starts with a project prefix (e.g. "nc-"), so several projects can
share one Redis database. Talks to Redis itself (RESP over a socket): no
redis-cli and no Python module needed.

    chat.py [--db N] [--prefix P] [--host H] [--port N] <command> ...

    register <id> <what you work on>   enter yourself in <prefix>agents
    agents                             who works on what
    say <id> <to> <message>            post to the room; <to> = all or an id
    read <id> [--all]                  new messages for <id> since its cursor
                                       (--all: every message, from the start);
                                       moves the cursor
    watch <id>                         wait until a message for <id> arrives
                                       from someone else, print it, exit (run
                                       it in the background: the exit is the
                                       wake-up); doesn't move the cursor
    claim <id> <topic> [note]          claim a topic (exit 1 if someone else
                                       has it, and print who)
    unclaim <id> <topic>               give a claim up (only your own)
    claims                             all claims
    lock <id> <name> [--ttl S] [note]  take a lock, e.g. "pr-slot" (exit 1 if
                                       taken, and print by whom)
    unlock <id> <name>                 release a lock (only your own)

Defaults come from the environment: CHATROOM_DB (7), CHATROOM_PREFIX
(required, e.g. "nc-"), CHATROOM_HOST (127.0.0.1), CHATROOM_PORT (6379),
CHATROOM_PASSWORD (none).
Keys: <prefix>chatroom (stream: from, to, time, msg), <prefix>agents (hash),
<prefix>cursor (hash: id -> last read entry), <prefix>claim:<topic>,
<prefix>lock:<name>.
"""
import argparse
import os
import socket
import sys
import time


class RedisError(Exception):
    pass


class Redis:
    """The few bits of RESP2 this needs: commands as arrays of bulk strings,
    and the five reply types."""

    def __init__(self, host, port, db, password=None):
        self.sock = socket.create_connection((host, port), timeout=10)
        self.sock.settimeout(None)  # XREAD BLOCK waits as long as it likes
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
    try:
        r = Redis(a.host, a.port, a.db, os.environ.get("CHATROOM_PASSWORD"))
    except OSError as e:
        die(f"can't connect to Redis at {a.host}:{a.port}: {e}", 3)
    args, p = a.args, a.prefix
    stream, agents, cursor = p + "chatroom", p + "agents", p + "cursor"

    def need(n, usage):
        if len(args) < n:
            die("usage: chat.py " + usage)

    if a.cmd == "register":
        need(2, "register <id> <what you work on>")
        r.cmd("HSET", agents, args[0], " ".join(args[1:]) + " | " + now())
    elif a.cmd == "agents":
        flat = r.cmd("HGETALL", agents) or []
        for k, v in sorted(zip(flat[0::2], flat[1::2])):
            print(f"{k}: {v}")
    elif a.cmd == "say":
        need(3, "say <id> <to> <message>")
        print(r.cmd("XADD", stream, "MAXLEN", "~", "1000", "*", "from", args[0],
                    "to", args[1], "time", now(), "msg", " ".join(args[2:])))
    elif a.cmd == "read":
        need(1, "read <id> [--all]")
        me, everything = args[0], "--all" in args[1:]
        if everything:
            es = entries(r.cmd("XRANGE", stream, "-", "+"))
        else:
            last = r.cmd("HGET", cursor, me) or "0-0"
            es = entries(r.cmd("XRANGE", stream, "(" + last, "+"))
        for eid, f in es:
            if everything or for_me(me, f):
                show(eid, f)
        if es:
            r.cmd("HSET", cursor, me, es[-1][0])
    elif a.cmd == "watch":
        need(1, "watch <id>")
        me = args[0]
        last = r.cmd("HGET", cursor, me) or "0-0"
        while True:
            reply = r.cmd("XREAD", "BLOCK", "600000", "COUNT", "100", "STREAMS", stream, last)
            if not reply:
                continue  # timed out: wait again
            es = entries(reply[0][1])
            last = es[-1][0]
            hits = [(eid, f) for eid, f in es if for_me(me, f)]
            if hits:
                for eid, f in hits:
                    show(eid, f)
                print(f"(then: chat.py read {me}, which also moves your cursor)")
                return
    elif a.cmd in ("claim", "lock"):
        ttl = None
        if a.cmd == "lock" and "--ttl" in args:
            i = args.index("--ttl")
            ttl = args[i + 1]
            args = args[:i] + args[i + 2:]
        need(2, f"{a.cmd} <id> <{'topic' if a.cmd == 'claim' else 'name'}> [note]")
        k = f"{p}{a.cmd}:{args[1]}"
        val = args[0] + (" " + " ".join(args[2:]) if len(args) > 2 else "") + " | " + now()
        if r.cmd("SET", k, val, "NX", *(["EX", ttl] if ttl else [])) != "OK":
            print(f"taken: {r.cmd('GET', k)}")
            sys.exit(1)
    elif a.cmd in ("unclaim", "unlock"):
        need(2, f"{a.cmd} <id> <topic>")
        k = f"{p}{'claim' if a.cmd == 'unclaim' else 'lock'}:{args[1]}"
        cur = r.cmd("GET", k)
        if cur and cur.split(" ")[0] != args[0]:
            print(f"not yours: {cur}")
            sys.exit(1)
        r.cmd("DEL", k)
    elif a.cmd == "claims":
        cur, keys = "0", []
        while True:
            cur, batch = r.cmd("SCAN", cur, "MATCH", p + "claim:*", "COUNT", "1000")
            keys += batch
            if cur == "0":
                break
        for k in sorted(set(keys)):
            print(f"{k[len(p + 'claim:'):]}: {r.cmd('GET', k)}")
    else:
        die(f"unknown command {a.cmd!r}; see chat.py --help")


if __name__ == "__main__":
    try:
        main()
    except RedisError as e:
        die(f"redis: {e}", 3)
