#!/usr/bin/env -S python3 -I
"""A read-only web view of the chat.py rooms: the rooms, who is in them, and
their messages as they arrive.

    python3 -I contrib/web.py [--listen 127.0.0.1:8888] [--db 7] [--host H] [--port P]

Then open http://127.0.0.1:8888/. It reads Redis with chat.py's own client and
never writes: no cursor moves, so nobody's unread messages change. It listens
on 127.0.0.1 only by default and answers only requests whose Host header is a
local name, so a web page from elsewhere cannot read the rooms through your
browser. CHATROOM_DB, CHATROOM_HOST, CHATROOM_PORT and CHATROOM_PASSWORD are
read as by chat.py. The page loads Bootstrap 5.3.8 and its icons
(the pugleaf.net look) from cdn.jsdelivr.net, pinned by SRI hash.
"""
import argparse
import importlib.util
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

_here = os.path.dirname(os.path.realpath(__file__))
_spec = importlib.util.spec_from_file_location(
    "chat", os.path.join(os.path.dirname(_here), "chat.py"))
chat = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chat)

ID_RE = re.compile(r"^\d+-\d+$")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
CFG = {}


def redis():
    return chat.Redis(CFG["host"], CFG["port"], CFG["db"],
                      os.environ.get("CHATROOM_PASSWORD"), timeout=5)


def hash_of(r, key):
    flat = r.cmd("HGETALL", key) or []
    return dict(zip(flat[0::2], flat[1::2]))


def scan_all(r, pattern):
    cur, keys = "0", set()
    while True:
        cur, batch = r.cmd("SCAN", cur, "MATCH", pattern, "COUNT", "1000")
        keys.update(batch)
        if cur == "0":
            return keys


def room_list(r):
    """Every prefix that has a stream or an agents hash."""
    ps = set()
    for k in scan_all(r, "*-chatroom"):
        ps.add(k[:-len("chatroom")])
    for k in scan_all(r, "*-agents"):
        ps.add(k[:-len("agents")])
    return sorted(ps)


def message(eid, f, prefix=None):
    m = {"id": eid, "from": f.get("from", ""), "to": f.get("to", ""),
         "time": f.get("time", ""), "msg": f.get("msg", "")}
    if prefix is not None:
        m["room"] = prefix
    return m


def rooms_summary(r):
    out = []
    for p in room_list(r):
        last = chat.entries(r.cmd("XREVRANGE", p + "chatroom", "+", "-", "COUNT", "1"))
        out.append({
            "prefix": p,
            "messages": r.cmd("XLEN", p + "chatroom"),
            "online": r.cmd("HLEN", p + "agents"),
            "watching": len(scan_all(r, chat.glob_escape(p) + "watching:*")),
            "last": message(*last[0]) if last else None,
        })
    out.sort(key=lambda x: chat.sid(x["last"]["id"]) if x["last"] else (0, 0), reverse=True)
    return out


def held(r, p, kind):
    res = []
    for k in sorted(scan_all(r, chat.glob_escape(p) + kind + ":*")):
        v = r.cmd("GET", k)
        if v is None:
            continue
        owner, _, note = v.partition(" ")
        res.append({"name": k[len(p + kind + ":"):], "owner": owner, "note": note,
                    "ttl": r.cmd("TTL", k)})
    return res


def agents(r, p):
    online = hash_of(r, p + "agents")
    models = hash_of(r, p + "model")
    kinds = hash_of(r, p + "session-kind")
    seen = hash_of(r, p + "last-seen")
    cursors = hash_of(r, p + "cursor")
    ids = sorted(set(online) | set(seen))
    watch = dict(zip(ids, r.cmd("MGET", *[chat.watching_key(p, i) for i in ids]) or [])) if ids else {}
    res = []
    for i in ids:
        desc, _, since = online.get(i, "").rpartition(" | ")
        if not desc:
            desc, since = online.get(i, ""), ""
        hits, unsure = chat.unread(r, p + "chatroom", i, cursors.get(i, "0-0"))
        res.append({
            "id": i, "online": i in online, "description": desc, "since": since,
            "model": models.get(i, ""), "kind": kinds.get(i, ""),
            "last_seen": int(seen[i]) if seen.get(i, "").isdigit() else None,
            "watching": watch.get(i), "unread": len(hits), "unread_more": bool(unsure),
        })
    return res


def room(r, p, after, before, limit):
    stream = p + "chatroom"
    if after:
        es = chat.entries(r.cmd("XRANGE", stream, "(" + after, "+", "COUNT", limit))
    else:
        es = chat.entries(r.cmd("XREVRANGE", stream, "(" + before if before else "+", "-",
                                "COUNT", limit))[::-1]
    return {"prefix": p, "messages": [message(*e) for e in es],
            "agents": agents(r, p), "claims": held(r, p, "claim"), "locks": held(r, p, "lock"),
            "total": r.cmd("XLEN", stream)}


def flow(r, after, limit):
    """The newest messages of all rooms, merged by time."""
    res = []
    for p in room_list(r):
        stream = p + "chatroom"
        if after:
            es = chat.entries(r.cmd("XRANGE", stream, "(" + after, "+", "COUNT", limit))
        else:
            es = chat.entries(r.cmd("XREVRANGE", stream, "+", "-", "COUNT", limit))
        res += [message(eid, f, p) for eid, f in es]
    res.sort(key=lambda m: chat.sid(m["id"]))
    return {"messages": res if after else res[-limit:]}


class Handler(BaseHTTPRequestHandler):
    server_version = "chat-web"

    def log_message(self, fmt, *args):
        if CFG.get("verbose"):
            super().log_message(fmt, *args)

    def send(self, code, body, ctype):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'unsafe-inline'; "
                         "style-src 'unsafe-inline' https://cdn.jsdelivr.net; "
                         "font-src https://cdn.jsdelivr.net; connect-src 'self'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        host = re.sub(r":\d+$", "", self.headers.get("Host") or "")
        if host not in LOCAL_HOSTS and host != CFG["bind"]:
            return self.send(403, "forbidden host\n", "text/plain")
        url = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/":
                return self.send(200, PAGE, "text/html; charset=utf-8")
            limit = max(1, min(int(q.get("limit", "200")), 1000))
            after, before = q.get("after", ""), q.get("before", "")
            if (after and not ID_RE.match(after)) or (before and not ID_RE.match(before)):
                return self.send(400, '{"error":"bad id"}', "application/json")
            r = redis()
            try:
                if url.path == "/api/rooms":
                    body = {"rooms": rooms_summary(r), "now": int(time.time()),
                            "redis": f"redis://{CFG['host']}:{CFG['port']}/{CFG['db']}"}
                elif url.path == "/api/room":
                    p = q.get("p", "")
                    if p not in room_list(r):
                        return self.send(404, '{"error":"no such room"}', "application/json")
                    body = room(r, p, after, before, limit)
                elif url.path == "/api/flow":
                    body = flow(r, after, limit)
                else:
                    return self.send(404, '{"error":"not found"}', "application/json")
            finally:
                r.sock.close()
            self.send(200, json.dumps(body), "application/json")
        except (chat.RedisError, OSError) as e:
            self.send(502, json.dumps({"error": f"redis: {e}"}), "application/json")
        except ValueError:
            self.send(400, '{"error":"bad parameter"}', "application/json")


PAGE = r"""<!doctype html>
<html lang="en" data-bs-theme="dark">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Chatroom Viewer</title>
  <script>
    (function () {
      var t = null;
      try { t = localStorage.getItem('pl-theme'); } catch (e) {}
      if (!t) t = window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
      document.documentElement.setAttribute('data-bs-theme', t);
    })();
  </script>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css" integrity="sha384-sRIl4kxILFvY47J16cr9ZwB07vP4J8+LH7qKQnuqkuIAvNWLzeN8tE5YBujZqJLB" crossorigin="anonymous">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.13.1/font/bootstrap-icons.min.css" integrity="sha384-CK2SzKma4jA5H/MXDUU7i1TqZlCFaD4T01vtyDFvPlD97JQyS+IsSh1nI2EFbpyk" crossorigin="anonymous">
  <style>
    :root { --pl-accent: #62c4ff; }
    [data-bs-theme="dark"] {
      --bs-body-bg: #1c1c1f;
      --bs-body-color: #e8e9ed;
      --bs-secondary-bg: #26262a;
      --bs-tertiary-bg: #222225;
      --bs-link-color-rgb: 98, 196, 255;
      --bs-link-hover-color-rgb: 150, 215, 255;
    }
    body { background: var(--bs-body-bg); }
    .hero {
      background:
        radial-gradient(circle at 15% 20%, rgba(98, 196, 255, .14), transparent 45%),
        radial-gradient(circle at 85% 0%, rgba(255, 60, 116, .10), transparent 40%),
        var(--bs-tertiary-bg);
      border-bottom: 1px solid var(--bs-border-color);
    }
    .prompt::before { content: "$ "; color: var(--pl-accent); }
    .navbar-brand .bi, .section-title .bi { color: var(--pl-accent); }
    .section-title { display: flex; align-items: center; gap: .5rem; font-size: .8rem; text-transform: uppercase;
      letter-spacing: .06em; margin: 0; font-weight: 600; }
    .pane { overflow: auto; }
    @media (min-width: 992px) {
      .pane { height: calc(100vh - var(--top, 190px) - 2.5rem); }
    }
    .room { cursor: pointer; transition: border-color .15s; border-left: 3px solid transparent !important; }
    .room:hover { border-left-color: var(--bs-border-color) !important; }
    .room.active { border-left-color: var(--pl-accent) !important; background: var(--bs-secondary-bg); color: inherit; }
    .room .last { font-size: .78rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .dot { display: inline-block; width: .55rem; height: .55rem; border-radius: 50%; background: var(--bs-secondary-color); flex: none; }
    .dot.on { background: var(--bs-warning); }
    .dot.w { background: var(--bs-success); box-shadow: 0 0 .4rem rgba(25, 135, 84, .7); }
    .chip { cursor: pointer; font-weight: 600; }
    .msg { border-bottom: 1px solid var(--bs-border-color); padding: .55rem .9rem; }
    .msg.hide { display: none; }
    .msg.new { animation: flash 2.5s ease-out; }
    @keyframes flash { from { background: rgba(98, 196, 255, .14); } to { background: transparent; } }
    .msg .meta { font-size: .78rem; }
    .msg pre { white-space: pre-wrap; word-break: break-word; font-size: .82rem; margin: .3rem 0 0; color: var(--bs-body-color); }
    .msg pre.clip { max-height: 16em; overflow: hidden; -webkit-mask-image: linear-gradient(#000 70%, transparent); mask-image: linear-gradient(#000 70%, transparent); }
    .ag { padding: .45rem 0; border-bottom: 1px solid var(--bs-border-color); }
    .ag:last-child { border-bottom: 0; }
    .card-header .badge { font-weight: 500; }
  </style>
</head>
<body>
<nav class="navbar navbar-expand sticky-top bg-body-tertiary border-bottom">
  <div class="container-fluid px-4 gap-2 flex-wrap">
    <a class="navbar-brand fw-semibold" href="#"><i class="bi bi-chat-square-text"></i> chatrooms</a>
    <span class="small text-body-secondary font-monospace me-auto" id="status"></span>
    <div class="form-check form-switch mb-0 small">
      <input class="form-check-input" type="checkbox" role="switch" id="live" checked>
      <label class="form-check-label" for="live">live</label>
    </div>
    <div class="input-group input-group-sm" style="width: 16rem">
      <span class="input-group-text"><i class="bi bi-search"></i></span>
      <input class="form-control" id="q" placeholder="filter text or agent" autocomplete="off">
    </div>
    <button type="button" class="btn btn-sm btn-outline-secondary" id="themeToggle" aria-label="Toggle light/dark theme"><i class="bi bi-circle-half"></i></button>
  </div>
</nav>

<header class="hero py-3" id="hero">
  <div class="container-fluid px-4">
    <p class="font-monospace text-body-secondary mb-1 prompt small" id="addr">chat.py</p>
    <div class="d-flex flex-wrap align-items-center gap-2">
      <h1 class="h4 fw-bold mb-0 me-2">Agent chatrooms</h1>
      <span class="badge rounded-pill text-bg-primary"><i class="bi bi-door-open"></i> <span id="nRooms">0</span> rooms</span>
      <span class="badge rounded-pill text-bg-secondary"><i class="bi bi-person-check"></i> <span id="nOnline">0</span> online</span>
      <span class="badge rounded-pill text-bg-secondary"><i class="bi bi-ear"></i> <span id="nWatch">0</span> watching</span>
      <span class="badge rounded-pill text-bg-secondary"><i class="bi bi-eye"></i> read-only</span>
    </div>
  </div>
</header>

<main class="container-fluid px-4 py-3">
  <div class="row g-3">
    <div class="col-lg-3">
      <div class="card">
        <div class="card-header"><h2 class="section-title"><i class="bi bi-hdd-network"></i> Rooms</h2></div>
        <div class="list-group list-group-flush pane" id="rooms"></div>
      </div>
    </div>
    <div class="col-lg-6">
      <div class="card">
        <div class="card-header d-flex align-items-center gap-2">
          <h2 class="section-title"><i class="bi bi-chat-dots"></i> <span id="title">Messages</span></h2>
          <span class="ms-auto small" id="filt"></span>
        </div>
        <div class="pane" id="flow">
          <div class="text-center py-2"><button id="more" class="btn btn-sm btn-outline-secondary" hidden><i class="bi bi-arrow-up"></i> load older</button></div>
          <div id="msgs"></div>
        </div>
      </div>
    </div>
    <div class="col-lg-3">
      <div class="pane d-flex flex-column gap-3" id="side"></div>
    </div>
  </div>
</main>

<script>
"use strict";
const $ = s => document.querySelector(s);
const el = (t, c, txt) => { const e = document.createElement(t); if (c) e.className = c; if (txt != null) e.textContent = txt; return e; };
const icon = n => el("i", "bi bi-" + n);
const ALL = "*";
let cur = null, first = null, last = null, busy = false, agentFilter = null;

function hue(s) { let h = 0; for (const c of s) h = (h * 31 + c.charCodeAt(0)) >>> 0; return h % 360; }
function dark() { return document.documentElement.getAttribute("data-bs-theme") === "dark"; }
function chip(id) {
  const c = el("span", "badge chip", id), h = hue(id);
  c.style.background = `hsl(${h} 70% 50% / .18)`;
  c.style.color = `hsl(${h} 75% ${dark() ? 72 : 32}%)`;
  c.style.border = `1px solid hsl(${h} 70% 50% / .35)`;
  c.title = "show only messages from or to " + id;
  c.onclick = e => { e.stopPropagation(); setAgent(id); };
  return c;
}
function tag(text, cls) { return el("span", "badge " + (cls || "text-bg-secondary"), text); }
function msOf(id) { return +id.split("-")[0]; }
function fmtTime(id) {
  const d = new Date(msOf(id));
  return new Date().toDateString() === d.toDateString()
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
    : d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
function ago(ms) {
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 60) return Math.round(s) + "s"; if (s < 3600) return Math.round(s / 60) + "m";
  if (s < 86400) return Math.round(s / 3600) + "h"; return Math.round(s / 86400) + "d";
}
async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.status);
  return r.json();
}
function status(t) { $("#status").textContent = t; }
function sizePanes() { document.documentElement.style.setProperty("--top", $("main").getBoundingClientRect().top + window.scrollY + "px"); }

async function loadRooms() {
  const d = await api("/api/rooms");
  $("#addr").textContent = d.redis;
  $("#nRooms").textContent = d.rooms.length;
  $("#nOnline").textContent = d.rooms.reduce((a, r) => a + r.online, 0);
  $("#nWatch").textContent = d.rooms.reduce((a, r) => a + r.watching, 0);
  const box = $("#rooms"); box.replaceChildren();
  const all = el("a", "list-group-item list-group-item-action room" + (cur === ALL ? " active" : ""));
  const ah = el("div", "d-flex align-items-center gap-2 fw-semibold"); ah.append(icon("collection"), document.createTextNode("All rooms"));
  all.append(ah, el("div", "last text-body-secondary", "newest messages of every room"));
  all.onclick = () => pick(ALL); box.append(all);
  for (const r of d.rooms) {
    const e = el("a", "list-group-item list-group-item-action room" + (r.prefix === cur ? " active" : ""));
    const h = el("div", "d-flex align-items-center gap-2");
    h.append(el("span", "dot" + (r.watching ? " w" : r.online ? " on" : "")), el("span", "fw-semibold font-monospace", r.prefix.replace(/-$/, "")));
    const n = el("span", "ms-auto d-flex gap-1");
    if (r.watching) n.append(tag(r.watching + " w", "text-bg-success"));
    n.append(tag(String(r.messages)));
    h.append(n);
    e.append(h, el("div", "last text-body-secondary", r.last ? `${ago(msOf(r.last.id))} · ${r.last.from}: ${r.last.msg.slice(0, 100)}` : "no messages"));
    e.title = `${r.prefix}: ${r.online} online, ${r.watching} watching, ${r.messages} messages`;
    e.onclick = () => pick(r.prefix); box.append(e);
  }
}

function renderMsg(m, fresh) {
  const e = el("div", "msg" + (fresh ? " new" : ""));
  e.dataset.from = m.from; e.dataset.to = m.to; e.dataset.text = (m.from + " " + m.to + " " + m.msg).toLowerCase();
  const h = el("div", "meta d-flex flex-wrap align-items-center gap-1");
  const t = el("span", "font-monospace text-body-secondary me-1", fmtTime(m.id)); t.title = m.id + "  " + m.time; h.append(t);
  if (m.room !== undefined) { const rt = tag(m.room.replace(/-$/, ""), "text-bg-dark border font-monospace"); rt.style.cursor = "pointer"; rt.onclick = () => pick(m.room); h.append(rt); }
  h.append(chip(m.from), el("i", "bi bi-arrow-right text-body-secondary"));
  for (const to of m.to.split(",")) {
    if (to.startsWith("!")) h.append(tag("not " + to.slice(1), "text-bg-danger bg-opacity-50"));
    else if (to === "all") h.append(tag("all", "text-bg-primary"));
    else h.append(chip(to));
  }
  const b = el("pre", "font-monospace", m.msg);
  e.append(h, b);
  if (m.msg.split("\n").length > 12 || m.msg.length > 1400) {
    b.classList.add("clip");
    const x = el("button", "btn btn-link btn-sm p-0", "show all");
    x.onclick = () => { const c = b.classList.toggle("clip"); x.textContent = c ? "show all" : "show less"; };
    e.append(x);
  }
  applyFilter(e); return e;
}
function matches(e) {
  const q = $("#q").value.trim().toLowerCase();
  if (agentFilter && ![e.dataset.from, ...e.dataset.to.split(",")].includes(agentFilter)) return false;
  return !q || e.dataset.text.includes(q);
}
function applyFilter(e) { e.classList.toggle("hide", !matches(e)); }
function refilter() {
  document.querySelectorAll(".msg").forEach(applyFilter);
  const f = $("#filt"); f.replaceChildren();
  if (agentFilter) {
    const b = el("button", "btn btn-sm btn-outline-secondary py-0"); b.append(icon("x"));
    b.title = "clear"; b.onclick = () => setAgent(null);
    f.append(el("span", "text-body-secondary me-1", "only"), chip(agentFilter), document.createTextNode(" "), b);
  }
}
function setAgent(id) { agentFilter = agentFilter === id ? null : id; refilter(); }

function nearBottom() {
  const p = $("#flow");
  if (p.scrollHeight > p.clientHeight) return p.scrollHeight - p.scrollTop - p.clientHeight < 80;
  return document.documentElement.scrollHeight - window.scrollY - window.innerHeight < 80;
}
function scrollEnd() { const p = $("#flow"); p.scrollTop = p.scrollHeight; }

function card(iconName, title, count) {
  const c = el("div", "card"), h = el("div", "card-header d-flex align-items-center");
  const t = el("h2", "section-title"); t.append(icon(iconName), document.createTextNode(title)); h.append(t);
  if (count != null) h.append(tag(String(count), "text-bg-secondary ms-auto"));
  const b = el("div", "card-body py-2"); c.append(h, b); return [c, b];
}
function renderSide(d) {
  const s = $("#side"); s.replaceChildren();
  if (cur === ALL) {
    const [c, b] = card("info-circle", "All rooms");
    b.append(el("div", "small text-body-secondary", "Click a room tag on a message, or a room on the left, to see its agents, claims and locks."));
    s.append(c); return;
  }
  const [ac, ab] = card("people", "Agents", d.agents.length);
  if (!d.agents.length) ab.append(el("div", "small text-body-secondary", "none"));
  const order = a => a.watching ? 0 : a.online ? 1 : 2;
  for (const a of [...d.agents].sort((x, y) => order(x) - order(y) || x.id.localeCompare(y.id))) {
    const e = el("div", "ag"), i = el("div", "d-flex flex-wrap align-items-center gap-1");
    i.append(el("span", "dot me-1" + (a.watching ? " w" : a.online ? " on" : "")), chip(a.id));
    if (a.model) i.append(tag(a.model, "text-bg-info bg-opacity-25 text-info-emphasis"));
    i.append(tag(a.watching ? "watching" : a.online ? "not watching" : "offline",
      a.watching ? "text-bg-success" : a.online ? "text-bg-warning" : "text-bg-secondary"));
    if (a.unread) i.append(tag(`${a.unread}${a.unread_more ? "+" : ""} unread`, "text-bg-danger"));
    e.append(i);
    const desc = a.online ? a.description : (a.last_seen ? `last seen ${ago(a.last_seen * 1000)} ago` : "");
    if (desc) e.append(el("div", "small text-body-secondary mt-1", desc));
    if (a.watching) e.title = "watcher: " + a.watching;
    ab.append(e);
  }
  s.append(ac);
  for (const [name, ic, list] of [["Claims", "flag", d.claims], ["Locks", "lock", d.locks]]) {
    const [c, b] = card(ic, name, list.length);
    if (!list.length) b.append(el("div", "small text-body-secondary", "none"));
    for (const x of list) {
      const e = el("div", "ag"), i = el("div", "d-flex flex-wrap align-items-center gap-1");
      i.append(el("code", "small", x.name), chip(x.owner));
      if (x.ttl > 0) i.append(tag(`${ago(Date.now() - x.ttl * 1000)} left`));
      e.append(i);
      if (x.note) e.append(el("div", "small text-body-secondary mt-1", x.note));
      b.append(e);
    }
    s.append(c);
  }
}

async function pick(p) {
  cur = p; first = last = null; agentFilter = null;
  $("#msgs").replaceChildren(); refilter();
  $("#title").textContent = p === ALL ? "All rooms" : p.replace(/-$/, "");
  try { history.replaceState(null, "", "#" + encodeURIComponent(p)); } catch (e) {}
  await loadRooms().catch(e => status("error: " + e.message));
  await poll(true);
}

async function poll(initial) {
  if (busy || cur == null) return;
  busy = true; const p = cur;
  try {
    const q = `limit=${initial ? 200 : 500}` + (last && !initial ? `&after=${last}` : "");
    const d = await api(p === ALL ? `/api/flow?${q}` : `/api/room?p=${encodeURIComponent(p)}&${q}`);
    if (p !== cur) return;
    const stick = initial || nearBottom(), box = $("#msgs");
    for (const m of d.messages) { box.append(renderMsg(m, !initial)); last = m.id; if (!first) first = m.id; }
    if (p !== ALL || initial) renderSide(d);
    $("#more").hidden = p === ALL || !first || d.total <= box.children.length;
    if (stick) scrollEnd();
    status(`updated ${new Date().toLocaleTimeString()}`);
  } catch (e) { status("error: " + e.message); } finally { busy = false; }
}

$("#more").onclick = async () => {
  if (!first || cur === ALL) return;
  const d = await api(`/api/room?p=${encodeURIComponent(cur)}&limit=200&before=${first}`);
  const box = $("#msgs"), p = $("#flow"), h = p.scrollHeight;
  for (const x of d.messages.reverse()) { box.prepend(renderMsg(x, false)); first = x.id; }
  p.scrollTop += p.scrollHeight - h;
  $("#more").hidden = !d.messages.length || d.total <= box.children.length;
};
$("#q").oninput = refilter;
$("#themeToggle").onclick = () => {
  const r = document.documentElement, next = r.getAttribute("data-bs-theme") === "dark" ? "light" : "dark";
  r.setAttribute("data-bs-theme", next);
  try { localStorage.setItem("pl-theme", next); } catch (e) {}
  pick(cur);
};
window.addEventListener("resize", sizePanes);
setInterval(() => { if ($("#live").checked && !document.hidden) poll(false); }, 2000);
setInterval(() => { if ($("#live").checked && !document.hidden) loadRooms().catch(() => {}); }, 10000);
sizePanes();
pick(decodeURIComponent(location.hash.slice(1)) || ALL);
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="Read-only web view of the chat.py rooms.")
    ap.add_argument("--listen", default="127.0.0.1:8888", help="address:port to serve on")
    ap.add_argument("--db", type=int, default=int(os.environ.get("CHATROOM_DB", str(chat.DEFAULT_DB))))
    ap.add_argument("--host", default=os.environ.get("CHATROOM_HOST", chat.DEFAULT_HOST))
    ap.add_argument("--port", type=int, default=int(os.environ.get("CHATROOM_PORT", str(chat.DEFAULT_PORT))))
    ap.add_argument("-v", "--verbose", action="store_true", help="log each request")
    a = ap.parse_args()
    bind, _, port = a.listen.rpartition(":")
    CFG.update(db=a.db, host=a.host, port=a.port, bind=bind or "127.0.0.1", verbose=a.verbose)
    redis().sock.close()  # fail now, not on the first page load
    srv = ThreadingHTTPServer((CFG["bind"], int(port)), Handler)
    print(f"chatroom viewer on http://{a.listen}/ (Redis {a.host}:{a.port} db {a.db}, read-only)",
          file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
