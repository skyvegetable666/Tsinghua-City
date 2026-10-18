# Changelog

This is a compact milestone log; Git history remains authoritative.

| Commit | Milestone |
| --- | --- |
| `8496397` | Phase 1 world-state service, MCP tools, CLI, SQLite/Postgres schema |
| `c245dd2` | Idempotency for transfers/life/messages; MCP stdio test |
| `cc9642c` | Task board with escrow, Alembic baseline, conditional Postgres concurrency tests |
| `8aef2a9` | Public bulletin board; explicit 0001 baseline + 0002 |
| `e260d35` | Task publication idempotency, maintenance scheduler, 0003 |
| `2284fcc` | Phase 3 local runtime manager, token rotation, MCP config, logs, reconcile, 0004 |
| `86a59c0` | Wake suspended runtime on unread messages |
| `17af044` | Fix Postgres stale-lock double-spend; bypass loopback proxies in MCP tests/sidecar |
| `260344e` | On-failure runtime restart with backoff, 0005 |
| `9e4c066` | Terminate crashed runtime to cancel restart |

## Post-alpha additions

- deterministic reference MCP harness (`reference_harness.py`)
- deterministic participation policy (`harness_policy.py`)
- two-agent demo (`scripts/two_agent_demo.py`)
- MCP list-content decoding regression test
- early Canvas at `/canvas` with admin board-read API
- local count: 35 passed / 2 skipped; Postgres expected 37 passed

## Verification snapshots

- At `17af044`: Windows 26 passed / 2 skipped; Postgres 28 passed; double-spend 5/5.
- At `9e4c066`: Windows 29 passed / 2 skipped; Postgres 31 passed.
- Reference harness + Canvas at current HEAD: local 35 passed / 2 skipped; manual two-agent demo completed four tasks; Windows/Postgres not yet rerun.
- The old `genesis-lab` prototype was archived after Phase 3 local runtime; it has no runtime imports.
