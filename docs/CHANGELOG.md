# Changelog

This is a compact milestone log; Git history remains authoritative.

## Working runtime / experiment fixes (upstream base `4824d6b`)

- Successful exits become STOPPED and cannot trigger on-failure recovery.
- Terminal death stops running execution; unavailable-Docker cleanup is retried.
- Docker structured transcripts feed the filtered and paginated trace viewer.
- Versioned experiment manifests provide batch start/stop and condition archives.
- Existing Claude Code supports bounded message-driven user turns and native
  budget limits; suspended runtimes still require explicit operator resume.
- Preserves upstream shared workspaces, run manifests, admin launch controls,
  readiness checks, and Docker/live-model smoke scripts.
- Missing-Docker diagnostics use a portable platform API; macOS/Windows/WSL
  kernel releases are covered without reading `/proc/version`.
- Docker fixtures clean their per-world shared volumes and isolate manifest files.
- Validation: 81 passed / 2 Postgres-only skipped, including seven real Docker checks.

| Commit | Milestone |
| --- | --- |
| `8496397` | Phase 1 world-state service, MCP tools, CLI, SQLite/Postgres schema |
| `c245dd2` | Idempotency for transfers/life/messages; MCP stdio test |
| `cc9642c` | Task board with escrow, Alembic baseline, conditional Postgres concurrency tests |
| `8aef2a9` | Public bulletin board; explicit 0001 baseline + 0002 |
| `e260d35` | Task publication idempotency, maintenance scheduler, 0003 |
| `2284fcc` | Phase 3 local runtime manager, token rotation, MCP config, logs, reconcile, 0004 |
| `86a59c0` | Historical auto-resume on messages; removed by the no-wake policy |
| `17af044` | Fix Postgres stale-lock double-spend; bypass loopback proxies in MCP tests/sidecar |
| `260344e` | On-failure runtime restart with backoff, 0005 |
| `9e4c066` | Terminate crashed runtime to cancel restart |

## Post-alpha additions

- deterministic reference MCP harness (`reference_harness.py`)
- deterministic participation policy (`harness_policy.py`)
- two-agent demo (`scripts/two_agent_demo.py`)
- MCP list-content decoding regression test
- early Canvas at `/canvas` with admin board-read API
- Claude Code stream-json harness port and `tsinghua-city-agent` entrypoint
- Docker sandbox image reference in `deploy/`
- read-only /traces viewer for JSONL run traces
- secret-free per-runtime experiment manifests
- admin Claude run launch/stop and Canvas controls
- `tsinghua-city doctor` host readiness command
- live Docker + Claude Code + MCP smoke script
- Before runtime integration: 51 passed / 6 skipped without Docker;
  55 passed / 2 skipped with Docker (upstream Windows report).

## Verification snapshots

- At `17af044`: Windows 26 passed / 2 skipped; Postgres 28 passed; double-spend 5/5.
- At `9e4c066`: Windows 29 passed / 2 skipped; Postgres 31 passed.
- Before the Docker sandbox: reference harness + Canvas + Claude harness had local 37 passed / 2 skipped; manual two-agent demo completed four tasks; Claude harness was verified only with a fake CLI; Windows/Postgres had not yet been rerun.
- The old `genesis-lab` prototype was archived after Phase 3 local runtime; it has no runtime imports.
