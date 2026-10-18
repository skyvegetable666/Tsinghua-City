"""Host-side web console for a running Genesis world.

Runs in the same asyncio loop as the supervisor when started with
python -m supervisor up --web.  It is intentionally a thin local operator
surface: status, live event timeline, system messages, end, and report.

Security: the console has no authentication and can inject messages / end the
run, so it binds 127.0.0.1 by default.  Do not expose WEB_CONSOLE_HOST=0.0.0.0
on an untrusted network.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from aiohttp import web

from .broker import Broker
from .report import build_report
from .settings import RunContext, Settings

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Genesis Lab Console</title>
<style>
  :root {
    --bg: #111418; --panel: #181d24; --panel2: #1f2630; --border: #2e3744;
    --text: #d9e1ec; --dim: #8b98a8; --accent: #63b3ed; --ok: #68d391;
    --warn: #f6ad55; --bad: #fc8181; --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text); font: 14px/1.5 system-ui, sans-serif; }
  header { position: sticky; top: 0; z-index: 5; background: rgba(17,20,24,.96); border-bottom: 1px solid var(--border); padding: 14px 18px; display: flex; gap: 18px; align-items: center; flex-wrap: wrap; }
  h1 { font-size: 17px; margin: 0; letter-spacing: .2px; }
  .meta { color: var(--dim); font-family: var(--mono); font-size: 12px; }
  .meta b { color: var(--text); font-weight: 600; }
  .phase { padding: 2px 8px; border: 1px solid var(--border); border-radius: 999px; font-family: var(--mono); font-size: 12px; }
  .phase.running { color: var(--ok); border-color: #2f6b4b; }
  .phase.ending { color: var(--warn); border-color: #7a5525; }
  .phase.ended { color: var(--bad); border-color: #743535; }
  .spacer { flex: 1; }
  button { background: var(--panel2); color: var(--text); border: 1px solid var(--border); border-radius: 7px; padding: 7px 11px; cursor: pointer; font: inherit; }
  button:hover { border-color: var(--accent); }
  button.danger { color: var(--bad); border-color: #5e2d2d; }
  button:disabled { opacity: .45; cursor: not-allowed; }
  main { max-width: 1280px; margin: 0 auto; padding: 18px; display: grid; grid-template-columns: 1fr 360px; gap: 16px; }
  @media (max-width: 980px) { main { grid-template-columns: 1fr; } }
  section { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px; min-width: 0; }
  section h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .08em; color: var(--dim); margin: 0 0 10px; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; }
  .card { background: var(--panel2); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; }
  .card .name { font-weight: 700; }
  .card .state { color: var(--ok); font-family: var(--mono); font-size: 12px; }
  .card .state.offline, .card .state.stopped { color: var(--bad); }
  .card .row { color: var(--dim); font-family: var(--mono); font-size: 12px; margin-top: 4px; }
  .timeline { height: 58vh; min-height: 320px; overflow: auto; border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; background: #0d1116; }
  .event { border-bottom: 1px solid #1c232d; padding: 6px 0; font-family: var(--mono); font-size: 12px; white-space: pre-wrap; overflow-wrap: anywhere; }
  .event:last-child { border-bottom: 0; }
  .event .ts { color: #66758a; margin-right: 8px; }
  .event .kind { color: var(--accent); margin-right: 8px; }
  .event .msg { color: var(--text); }
  .event .delivered { color: var(--ok); }
  .event .sys { color: var(--warn); }
  .event .err { color: var(--bad); }
  .controls { display: grid; gap: 8px; }
  textarea, select, input { width: 100%; background: #0d1116; color: var(--text); border: 1px solid var(--border); border-radius: 7px; padding: 8px 9px; font: inherit; }
  textarea { min-height: 84px; resize: vertical; }
  .row { display: flex; gap: 8px; align-items: center; }
  .row > button { flex: 0 0 auto; }
  .toast { position: fixed; right: 18px; bottom: 18px; background: var(--panel2); border: 1px solid var(--border); border-radius: 8px; padding: 9px 12px; color: var(--text); opacity: 0; transition: opacity .2s; z-index: 30; max-width: 420px; }
  .toast.show { opacity: 1; }
  .toast.bad { border-color: #743535; color: var(--bad); }
  pre { margin: 0; padding: 10px; background: #0d1116; border: 1px solid var(--border); border-radius: 8px; overflow: auto; max-height: 34vh; color: #a9bdd4; font: 12px var(--mono); }
  .muted { color: var(--dim); }
  a { color: var(--accent); }
</style>
</head>
<body>
<header>
  <h1>Genesis Lab Console</h1>
  <div class="meta">run <b id="run-id">-</b></div>
  <div class="meta">agents <b id="agent-count">-</b></div>
  <span id="phase" class="phase">-</span>
  <div class="spacer"></div>
  <button id="report-btn">Report</button>
  <button id="end-btn" class="danger">End run</button>
</header>
<main>
  <div style="display:flex;flex-direction:column;gap:16px;min-width:0">
    <section>
      <h2>Agents</h2>
      <div id="cards" class="cards"><div class="muted">waiting for status...</div></div>
    </section>
    <section>
      <h2>Live timeline</h2>
      <div id="timeline" class="timeline"></div>
      <div class="meta" style="margin-top:8px"><a id="run-link" href="#">run directory</a></div>
    </section>
  </div>
  <div style="display:flex;flex-direction:column;gap:16px;min-width:0">
    <section>
      <h2>Operator message</h2>
      <div class="controls">
        <select id="target"><option value="all">all agents</option></select>
        <textarea id="say-text" placeholder="system message injected by the supervisor..."></textarea>
        <div class="row"><button id="say-btn">Send system message</button><span id="say-status" class="muted"></span></div>
      </div>
    </section>
    <section>
      <h2>Run report</h2>
      <pre id="report">Click Report to write/read transcript.md, metrics.json, messages.jsonl.</pre>
    </section>
  </div>
</main>
<div id="toast" class="toast"></div>
<script>
const $ = (id) => document.getElementById(id);
let lastAgentKey = "";
let toastTimer = null;

function toast(text, bad) {
  const el = $("toast");
  el.textContent = text;
  el.className = "toast show" + (bad ? " bad" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = "toast"; }, 3200);
}

function setText(id, value) {
  const el = $(id);
  if (el) el.textContent = value;
}

function eventText(e) {
  const kind = e.event || "event";
  if (kind === "message_sent") {
    const m = e.message || {};
    return "MESSAGE  " + (m.sender || "?") + " -> " + (m.recipient || "?") + "  " + (m.text || "");
  }
  if (kind === "message_delivered") {
    return "DELIVERED  " + (e.message_id || "?") + " via " + (e.channel || "?") + "  +" + (e.latency_s ?? "?") + "s";
  }
  if (kind === "agent_state") {
    return "STATE  " + (e.name || e.agent_id || "?") + "  " + (e.state || "?") + "  turn=" + (e.turn ?? "?");
  }
  if (kind === "agent_registered") {
    return "REGISTERED  " + (e.name || e.agent_id || "?") + "  model=" + (e.model || "?");
  }
  if (kind === "genesis") {
    const peers = Array.isArray(e.peers) ? e.peers : [];
    return "GENESIS  " + (e.name || "?") + "  peers=" + (peers.join(", ") || "?");
  }
  if (kind === "agent_trace") {
    const t = e.trace || {};
    if (t.type === "assistant_text") return "ASSISTANT [" + (e.agent_id || "?") + "]  " + (t.text || "");
    if (t.type === "tool_use") return "TOOL [" + (e.agent_id || "?") + "]  " + (t.name || "?");
    if (t.type === "turn_result") return "TURN [" + (e.agent_id || "?") + "]  #" + (t.turn ?? "?") + "  " + (t.stop_reason || "") + "  cost=" + (t.cost_usd ?? "?");
    return "TRACE [" + (e.agent_id || "?") + "]  " + (t.type || "?");
  }
  if (kind === "system_message_injected") {
    return "SYSTEM INJECTED  to=" + (e.to || "?") + "  " + (e.text || "");
  }
  if (kind === "run_started") return "RUN STARTED  " + (e.mode || "") + "  agents=" + (e.agent_count ?? "?");
  if (kind === "run_ending") return "RUN ENDING  " + (e.reason || "");
  if (kind === "run_ended") return "RUN ENDED  " + (e.reason || "");
  if (kind === "agent_offline") return "OFFLINE  " + (e.name || e.agent_id || "?") + "  " + (e.reason || "");
  if (kind === "agent_online") return "ONLINE  " + (e.name || e.agent_id || "?");
  return kind.toUpperCase() + "  " + JSON.stringify(e).slice(0, 900);
}

function appendEvent(e) {
  const box = $("timeline");
  const line = document.createElement("div");
  line.className = "event";
  const ts = document.createElement("span");
  ts.className = "ts";
  ts.textContent = (e.ts_iso || "").slice(11, 19) || new Date().toISOString().slice(11, 19);
  const kind = document.createElement("span");
  kind.className = "kind";
  kind.textContent = (e.event || "event").toUpperCase();
  const msg = document.createElement("span");
  msg.className = "msg";
  msg.textContent = eventText(e);
  line.appendChild(ts);
  line.appendChild(kind);
  line.appendChild(msg);
  box.appendChild(line);
  while (box.children.length > 600) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
}

function renderAgents(status) {
  const cards = $("cards");
  const agents = (status && status.agents) || {};
  const key = JSON.stringify(agents);
  if (key === lastAgentKey) return;
  lastAgentKey = key;
  cards.textContent = "";
  const entries = Object.entries(agents);
  if (!entries.length) {
    const empty = document.createElement("div");
    empty.className = "muted";
    empty.textContent = "no agents registered yet";
    cards.appendChild(empty);
    return;
  }
  for (const [id, a] of entries) {
    const card = document.createElement("div");
    card.className = "card";
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = (a.name || id) + "  (" + id + ")";
    card.appendChild(name);
    const state = document.createElement("div");
    state.className = "state " + String(a.state || "");
    state.textContent = (a.state || "?") + "  turn " + (a.turn ?? "?");
    card.appendChild(state);
    const row1 = document.createElement("div");
    row1.className = "row";
    row1.textContent = "connected=" + (a.connected ? "yes" : "no") + "  registered=" + (a.registered ? "yes" : "no");
    card.appendChild(row1);
    const row2 = document.createElement("div");
    row2.className = "row";
    row2.textContent = "model=" + (a.model || "?") + "  cost=$" + (a.cost_usd ?? "?");
    card.appendChild(row2);
    const u = a.usage || {};
    const row3 = document.createElement("div");
    row3.className = "row";
    row3.textContent = "tokens in=" + (u.input_tokens ?? 0) + " out=" + (u.output_tokens ?? 0);
    card.appendChild(row3);
    cards.appendChild(card);
  }
  const target = $("target");
  const current = target.value;
  const wanted = new Set(["all"]);
  target.textContent = "";
  const all = document.createElement("option");
  all.value = "all";
  all.textContent = "all agents";
  target.appendChild(all);
  for (const [id, a] of entries) {
    const opt = document.createElement("option");
    opt.value = a.name || id;
    opt.textContent = a.name || id;
    wanted.add(opt.value);
    target.appendChild(opt);
  }
  target.value = wanted.has(current) ? current : "all";
}

async function refreshStatus() {
  try {
    const r = await fetch("/api/status", {cache: "no-store"});
    const data = await r.json();
    const status = data.status || {};
    const phase = status.phase || "?";
    setText("run-id", status.run_id || "-");
    setText("agent-count", Object.keys(status.agents || {}).length);
    const phaseEl = $("phase");
    phaseEl.textContent = phase;
    phaseEl.className = "phase " + phase;
    const end = $("end-btn");
    end.disabled = phase !== "running";
    renderAgents(status);
    const link = $("run-link");
    if (data.run_dir) {
      link.textContent = data.run_dir;
      link.href = "file://" + data.run_dir;
    }
  } catch (e) {
    setText("phase", "offline");
  }
}

async function sendSystem() {
  const text = $("say-text").value.trim();
  if (!text) return;
  $("say-btn").disabled = true;
  setText("say-status", "sending...");
  try {
    const r = await fetch("/api/say", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({text: text, to: $("target").value})
    });
    const data = await r.json();
    if (!r.ok || data.ok === false) throw new Error(data.error || ("HTTP " + r.status));
    setText("say-status", "sent " + (data.message_ids || []).length + " message(s)");
    $("say-text").value = "";
    toast("system message injected");
  } catch (e) {
    setText("say-status", "failed");
    toast(String(e), true);
  } finally {
    $("say-btn").disabled = false;
  }
}

async function endRun() {
  if (!confirm("End the run gracefully? Agents get the configured grace period.")) return;
  $("end-btn").disabled = true;
  try {
    const r = await fetch("/api/end", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({reason: "ended from web console"})
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
    toast(data.started ? "ending run" : "run is not running");
  } catch (e) {
    toast(String(e), true);
    $("end-btn").disabled = false;
  }
}

async function report() {
  $("report").textContent = "generating...";
  try {
    const r = await fetch("/api/report", {cache: "no-store"});
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
    $("report").textContent = JSON.stringify(data.metrics || data, null, 2);
    toast("report written");
  } catch (e) {
    $("report").textContent = String(e);
    toast(String(e), true);
  }
}

function connectEvents() {
  const es = new EventSource("/api/events/stream?since=0");
  es.onmessage = (ev) => {
    try { appendEvent(JSON.parse(ev.data)); } catch (e) { /* ignore malformed */ }
  };
  es.onerror = () => {
    // EventSource reconnects automatically; status poll shows liveness.
    setText("phase", "event stream reconnecting");
  };
}

$("say-btn").addEventListener("click", sendSystem);
$("end-btn").addEventListener("click", endRun);
$("report-btn").addEventListener("click", report);
$("say-text").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) sendSystem();
});

refreshStatus();
setInterval(refreshStatus, 1500);
connectEvents();
</script>
</body>
</html>
"""


class WebConsole:
    def __init__(self, settings: Settings, run: RunContext, broker: Broker) -> None:
        self.settings = settings
        self.run = run
        self.broker = broker
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._stop = asyncio.Event()
        self.url = ""

    @property
    def events_path(self) -> Path:
        return self.run.run_dir / "events.jsonl"

    def make_app(self) -> web.Application:
        app = web.Application(client_max_size=1 * 1024 * 1024)
        app.router.add_get("/", self.h_index)
        app.router.add_get("/api/status", self.h_status)
        app.router.add_post("/api/say", self.h_say)
        app.router.add_post("/api/end", self.h_end)
        app.router.add_get("/api/events", self.h_events)
        app.router.add_get("/api/events/stream", self.h_stream)
        app.router.add_get("/api/report", self.h_report)
        return app

    async def start(self) -> None:
        self._runner = web.AppRunner(self.make_app(), access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner,
            self.settings.web_host,
            self.settings.web_port,
        )
        await self._site.start()
        server = getattr(self._site, "_server", None)
        port = self.settings.web_port
        if port == 0 and server is not None and server.sockets:
            port = server.sockets[0].getsockname()[1]
        host = self.settings.web_host
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        self.url = f"http://{host}:{port}"

    async def stop(self) -> None:
        self._stop.set()
        if self._runner is not None:
            try:
                await asyncio.wait_for(self._runner.cleanup(), timeout=10)
            except (TimeoutError, Exception):
                pass

    # ------------------------------------------------------------- handlers
    async def h_index(self, request: web.Request) -> web.Response:
        return web.Response(text=INDEX_HTML, content_type="text/html")

    async def h_status(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "status": self.broker.status(),
                "run_dir": str(self.run.run_dir),
                "web_url": self.url,
            }
        )

    async def h_say(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)
        try:
            ids = await self.broker.inject_system_message(
                str(body.get("text") or ""),
                str(body.get("to") or "all"),
            )
        except RuntimeError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=409)
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response({"ok": True, "message_ids": ids})

    async def h_end(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        reason = str(body.get("reason") or "ended from web console").strip()
        started = await self.broker.request_end(reason)
        return web.json_response({"ok": True, "started": started, "phase": self.broker.phase})

    async def h_events(self, request: web.Request) -> web.Response:
        try:
            since = max(0, int(request.query.get("since", "0")))
        except ValueError:
            since = 0
        events = self._read_events(since)
        return web.json_response({"ok": True, "events": events, "next": since + len(events)})

    async def h_stream(self, request: web.Request) -> web.StreamResponse:
        try:
            since = max(0, int(request.query.get("since", "0")))
        except ValueError:
            since = 0
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)
        cursor = since
        try:
            while not self._stop.is_set():
                events = self._read_events(cursor)
                for event in events:
                    await response.write(
                        b"data: " + json.dumps(event, ensure_ascii=False).encode() + b"\n\n"
                    )
                cursor += len(events)
                if not events:
                    await response.write(b": ping\n\n")
                    await asyncio.sleep(0.4)
                else:
                    await asyncio.sleep(0.05)
            await response.write_eof()
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        except Exception:
            pass
        return response

    async def h_report(self, request: web.Request) -> web.Response:
        try:
            metrics = build_report(self.run.run_dir)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=500)
        return web.json_response({"ok": True, "metrics": metrics})

    # ---------------------------------------------------------------- events
    def _read_events(self, since: int) -> list[dict[str, Any]]:
        path = self.events_path
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(data, dict):
                        events.append(data)
        except OSError:
            return []
        return events[since:]
