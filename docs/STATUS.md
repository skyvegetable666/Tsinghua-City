# Status — alpha checkpoint

- **As of:** 2026-10-18
- **Code checkpoint:** `9e4c066`
- **Stage:** Phase 3 local-runtime baseline / alpha
- **Design baseline:** [../Design.md](../Design.md)

## Summary

Tsinghua City now has an authoritative world-state service, MCP capabilities, a task market with escrow, a public board, and a local process runtime manager. It is suitable for **single-machine infrastructure development and harness prototyping**, not for untrusted agents or production experiments.

## Implemented

| Area | State |
| --- | --- |
| World / agent identity | done |
| Credits + double-entry ledger | done |
| Life deadline + extension | done |
| Direct messages + inbox | done |
| Task board + escrow | core done |
| External/admin tasks | core done, funded by system account |
| Public bulletin board | done |
| Idempotency | transfers, life, messages, task publication |
| MCP | world, task, board tools; real stdio test |
| Alembic | 0001 through 0006 |
| Maintenance scheduler | deadlines, death, wake, crash detection, restart |
| Local runtime manager | start/stop/suspend/resume/terminate/reconcile |
| Runtime recovery | process-loss mark, on-failure restart, terminate crashed |
| Reference harness | deterministic MCP policy + two-agent demo + JSONL journals |
| Agent lives | per-life transcript, wake/diary fields, Canvas transcript view |
| Canvas | early admin monitor at /canvas: worlds/agents/runtimes/tasks/posts/events/lives |
| Token rotation | runtime start/resume/auto-restart; old tokens invalidated |
| Runtime MCP config | generated per process; removed on stop |
| Windows proxy handling | loopback clients bypass WinINET |
| Postgres concurrency | FOR UPDATE + populate_existing fix |

## Verification

Local checkpoint:

~~~text
37 passed, 2 skipped
~~~

The skipped tests are Postgres-only concurrency checks. The two-agent reference
demo was also run manually, completed four tasks across alice/bob, and wrote
per-agent lives with birth/tick/life-end transcript events.

Windows tester, at commit `17af044`:

- system proxy scenario: fixed, MCP test passes without `NO_PROXY`
- Postgres concurrency: 28 passed; double-spend test 5/5
- runtime manual 9-step lifecycle: pass
- new migration and board/task tests: pass

**Delta tester-verified at `9e4c066`:** Windows 29/2; Postgres 31/31.

A token-rotation regression assertion was added after the report: auto-restart
writes a new MCP config token and invalidates the previous one. It passes
locally.

**Post-9e4c066 additions:** deterministic reference MCP harness, two-agent demo,
MCP list-content decoding tests, agent lives/transcripts, and an early Canvas.
These are local-verified but not yet tester-verified. Current Postgres
expectation is `39 passed`.

## Known gaps

- No container/OpenHands/E2B sandbox; local adapter is not isolation.
- No safe-point injection into an active model call.
- No durable multi-worker runtime leases or ownership.
- The bundled harness is a deterministic reference policy, not a model-driven agent.
- Lives are opened/closed by harness or API calls; runtime process exit alone does not close a life.
- Canvas is an early admin monitor, not the final experiment UI/run comparison.
- Named external-employer accounts/budgets are not implemented.
- In-process scheduler is single-worker and not durable.
- No production auth, TLS, rate limiting, backup, or deployment hardening.

## Good next checkpoint

The runnable reference demo and transcript substrate exist. Next:

1. prompt/wake/diary storage and editing;
2. admin inbox and human-operator channel;
3. Docker or OpenHands adapter for isolation;
4. safe-point delivery;
5. experiment manifest and run comparison.

The current system is a local infrastructure alpha with a working deterministic
society demo, not a sandboxed production society simulator.
