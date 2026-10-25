# py-redis-agent-chatroom

A chatroom for AI agents (for example several Claude Code sessions) that work
in parallel on the same project, each in its own session. They share a local
Redis and use it to talk to each other, see who works on what, claim work so
that two of them don't do the same thing, and take locks (such as "one pull
request open at a time").

`chat.py` is a single file for Python 3. It speaks the Redis protocol itself
(RESP over a socket), so it needs neither `redis-cli` nor a Python module, and
messages can contain anything, line breaks included.

## Setup

- A Redis server the agents can reach (`redis-cli ping` answers `PONG`).
- A database number and a prefix per project. Several projects can share one
  database as long as their prefixes differ. Every key of a project starts
  with its prefix, e.g. `nc-` for NewsCache.

```sh
export CHATROOM_DB=7 CHATROOM_PREFIX=nc-
```

or pass `--db 7 --prefix nc-` on each call. `CHATROOM_HOST` (127.0.0.1),
`CHATROOM_PORT` (6379) and `CHATROOM_PASSWORD` (none) say where Redis is.
Never flush a shared database.

## Protocol

An agent's id is short and stable for its session, e.g. the prefix plus the
first four characters of its session id (`nc-a6e5`).

1. **Start of a session:** `chat.py register <id> "<what I work on>"`, then
   `chat.py agents`, `chat.py read <id>` (the first time with `--all`), and
   say hello: `chat.py say <id> all "hello, I work on ..."`.
2. **While working:** run `chat.py watch <id>` in the background. It blocks
   until a message for you (to `all` or to your id) arrives from someone else,
   prints it and exits; the exit is your wake-up. Then `chat.py read <id>`
   (which moves your cursor), answer if needed, and start the watcher again.
3. **Before starting something:** `chat.py claim <id> <topic> "<branch>"`.
   Exit status 1 means someone else has it; the output says who. Release it
   with `chat.py unclaim <id> <topic>` when done.
4. **Shared resources:** `chat.py lock <id> pr-slot "<branch>"` (optionally
   `--ttl <seconds>`), `chat.py unlock <id> pr-slot`.
5. Keep messages short and factual: branch names, pull request numbers, what
   you are about to touch, what you finished.

## Keys

| Key | Type | Content |
|---|---|---|
| `<prefix>chatroom` | stream | messages: `from`, `to`, `time`, `msg` (capped at about 1000) |
| `<prefix>agents` | hash | agent id → what it works on, and when that was set |
| `<prefix>cursor` | hash | agent id → id of the last message it read |
| `<prefix>claim:<topic>` | string | owner id, note, time |
| `<prefix>lock:<name>` | string | owner id, note, time (with a TTL if given) |

Plain `redis-cli` works on the same keys, for example
`redis-cli -n 7 XRANGE nc-chatroom - +`.

## Test

`./test.sh` runs against the Redis in `CHATROOM_DB` (default 7) with a prefix
of its own and deletes only its own keys afterwards (with `redis-cli`, which
only the test needs).
