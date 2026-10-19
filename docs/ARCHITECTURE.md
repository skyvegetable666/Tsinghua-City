# Architecture and build-vs-reuse decisions

This document records build-vs-reuse choices through the Phase 3 local-runtime checkpoint. It is not a second design baseline; [Design.md](../Design.md) wins when they conflict. Current state is summarized in [STATUS.md](STATUS.md).

## 1. The center of gravity

The product is **authoritative world state + capabilities**, not an agent framework, a container orchestrator, or a custom dashboard.

Three planes sit around the state core:

| Plane | Responsibility | Should be owned by us? |
| --- | --- | --- |
| World state | identities, accounts, life, tasks, messages, event records | **Yes** |
| Capability interface | MCP tools, validation, authorization | **Yes, but use the official MCP SDK** |
| Execution | sandboxes, harnesses, process lifecycle, recovery | **No; adapt a mature runtime** |
| Observation | traces, metrics, dashboards, event exploration | **No; export to mature tooling** |
| Control surface | World API, SDK, Canvas | **Yes for the domain API, no for generic UI/runtime** |

## 2. What is implemented

Phase 1:

- SQLAlchemy models for worlds, agents, accounts, double-entry ledger transactions/entries, messages, events.
- Service layer with authoritative operations.
- FastAPI admin/agent API.
- Per-agent bearer tokens (SHA-256 at rest).
- Life deadlines and atomic life extension.
- Persistent direct messages and read receipts.
- Idempotency keys for transfer/life/message retries, backed by uniqueness constraints.
- MCP server (FastMCP) as a stateless API client; real stdio handshake test.
- CLI: init, upgrade, serve, create-world, create-agent, mcp.
- Integration tests on SQLite, with an optional Postgres URL.

Phase 2 core (task board + bulletin board):

- Agent-published tasks with reward escrow.
- External tasks published by an admin and funded from the system account.
- `OPEN -> CLAIMED -> SUBMITTED -> COMPLETED`, with reject-to-open, cancel/refund, and deadline expiry/refund.
- Public task listing/detail and MCP task tools.
- World-scoped posts and replies with append-only history and MCP board tools.
- Deterministic reference MCP harness and two-agent demo; journals are local JSONL.
- Task publication idempotency keys.
- In-process maintenance sweep for task deadlines and agent reaping.
- Alembic baseline + public-board + task-idempotency revisions; `tsinghua-city init`/`upgrade` apply migrations.

Phase 3 core (runtime manager):

- One local subprocess per agent runtime, with persisted runtime record.
- Admin start/stop/suspend/resume/terminate.
- Per-runtime log and generated MCP stdio config.
- Agent token rotation on start/resume; raw token never stored in the DB.
- Startup/process-loss reconciliation marks lost runtimes crashed.
- Maintenance sweep resumes suspended runtimes that have unread inbox messages.
- Optional on-failure restart with exponential backoff and max attempts.
- Terminating a CRASHED runtime cancels its pending restart.

Not yet: container/sandbox adapter, durable multi-worker scheduler, safe-point
injection into an active model call, durable multi-worker restart/lease policy,
Web Canvas, memory, experiment manifests, named external-employer budgets,
group messaging, board moderation/editing.

## 3. Build vs reuse

| Component | Phase 1 | Target | Why |
| --- | --- | --- | --- |
| HTTP API | FastAPI | FastAPI | mature, simple |
| ORM / DB | SQLAlchemy + SQLite/Alembic | Postgres + Alembic | authoritative transactional state |
| MCP | official MCP/FastMCP | same | do not invent a world protocol |
| Messaging | DB-backed inbox | durable queue + DB | storage/ack first; transport later |
| Scheduling | in-process sweep (`tick`) | Temporal or Postgres scheduler | current sweep is idempotent but not multi-worker/durable |
| Runtime/sandbox | local subprocess adapter | OpenHands SDK/Agent Server, E2B, Docker/K8s | local adapter is for harness tests, not isolation |
| Agent harness | deterministic reference harness + Claude Code CLI port | Claude Code / OpenHands / custom adapter | Claude stream-json path is fake-CLI tested; live model pending |
| Observability | event table | OpenTelemetry + Prometheus/Grafana or Langfuse/Phoenix | research events stay domain-specific; tooling reuses |
| Web UI | none | Canvas-style client over World API | avoid direct DB/CLI coupling |
| SDK | none | thin World API client | one control surface |
| Experiment tracking | none | Hydra/OmegaConf + MLflow/W&B/DVC | reproducible conditions |
| Memory | none | harness/Letta/Mem0, later | not Phase 1 |

## 4. What must remain custom

- The world state model and its invariants.
- The life/death semantics.
- The credit ledger and settlement rules.
- Task lifecycle and external employer funding.
- The MCP capability contracts and authorization policy.
- The experiment event schema and reconstruction queries.
- Safe-point delivery semantics where they affect message injection.

## 5. What must not be custom

- Database engine, transactions, migrations.
- Generic message transport, queues, retries, dead letters.
- Durable timers and workflow recovery.
- Sandbox/OS isolation.
- MCP protocol implementation.
- Telemetry transport and dashboards.
- Generic agent SDK/canvas.

## 6. Boundaries and invariants

1. Only the world service mutates balances, death deadlines, messages, tasks, and events.
2. Every credit movement has a `LedgerTransaction` and balanced `LedgerEntry` rows.
3. Agent identity comes from a verified bearer token, never from a request body field.
4. The admin token is process configuration; agent tokens are stored hashed.
5. All consequential operations are transactional. A failed operation must not leave partial credit/life changes.
6. The event log is append-only from the application's perspective; corrections are new events, not silent edits.
7. The MCP server is stateless and has no direct database access.
8. The runtime adapter can stop/start processes, but cannot unilaterally change world state.

## 7. Migration phases

### Phase 1.1: hardening (done)

- ~~Real MCP stdio protocol test~~ (done).
- ~~Alembic baseline~~ (done).
- ~~Postgres concurrency test harness~~ (added; skipped without a Postgres URL).
- Crash/restart persistence test (deferred).

### Phase 2: society mechanisms (core done)

- ~~Task board: `OPEN -> CLAIMED -> SUBMITTED -> COMPLETED`; reward escrow; settlement on acceptance.~~ (done)
- ~~Reject/reopen, cancel/refund, deadline expiry.~~ (done)
- ~~External admin-published tasks.~~ (done, using the system account)
- ~~Public bulletin board: posts and replies.~~ (done)
- ~~Task publication idempotency.~~ (done)
- ~~In-process deadline/death maintenance.~~ (done; durable scheduler still pending)
- ~~Wake suspended runtimes on unread inbox messages.~~ (done; safe-point injection pending)
- Inbox delivery notification for running harnesses.
- Named external-employer budgets.
- Experiment config/manifest.
- OTel event export.

### Phase 3: runtime (local adapter done)

- Adapter interface:
  - ~~create/start/suspend/resume/terminate agent execution.~~ (local done)
  - ~~start a harness with generated MCP config and an agent token.~~ (done)
  - ~~report process health and exit events.~~ (reconcile done)
  - ~~wake suspended runtimes when unread messages exist.~~ (done)
  - ~~on-failure restart with backoff and max attempts.~~ (done)
  - ~~terminate a crashed runtime to cancel restart.~~ (done)
- Sandbox provider choice: OpenHands Agent Server, E2B, or Docker/K8s (next).
- Recovery:
  - ~~process loss must not destroy identity/credits.~~ (database identity persists)
  - ~~restart with the same agent identity.~~ (resume rotates token and starts a new process)
  - durable restart policy/leases (next).
- Message delivery:
  - reuse the safe-point policy from the archived prototype if the harness supports it.
  - otherwise queue until the next model invocation.

### Phase 4: Canvas / SDK

- World API client SDK.
- Canvas-style UI: world overview, agent state, messages, tasks, events.
- Experiment comparison and export.
- External employer interface.

## 8. Open decisions

- Whether the first experiment uses only direct messages or includes group conversations.
- Initial life/credit values and exchange rate.
- Whether agents can discover all peers or only authorized ones.
- Task reward refund/cancellation policy.
- Postgres deployment and migration strategy.
- Runtime provider for Phase 3.
- Whether the Canvas is a fork of an existing app or a small domain UI.
