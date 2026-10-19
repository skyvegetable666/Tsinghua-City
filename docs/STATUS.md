# Status - experiment operations checkpoint

- Date: 2026-10-19
- Base: `ffc7d2437ae0794f97e6c39fc7fdbd3efdca306a`
- Local branch: `feat/experiment-operations-local`
- Review target: [PR #2](https://github.com/wangslzzz/Tsinghua-City/pull/2)
- Local agent image: `tsinghua-city-agent:operations-local` (select via `CITY_DOCKER_IMAGE`)
- Local API image: `tsinghua-city-api:operations-local`
- Packaged working-source SHA-256: `9493623346bd552a4c3e3891a3aea794507b50f26a2bd76b0eae57bfcdb82f98`

## Verified

| Check | Result |
| --- | --- |
| Full PostgreSQL + real Docker suite | 107 passed, zero skipped |
| Default SQLite suite | 94 passed, 13 optional checks skipped |
| Real Docker subset | 8 passed, including internal bridge and enforced tmpfs capacity |
| SQLite / PostgreSQL migrations | upgrade, downgrade, upgrade; existing world retained |
| Stale agent image | rejected before execution with a rebuild instruction |
| Real Claude multi-round run | 2 successful user turns, 2 real MCP tool results |
| Peer continuation | both expected markers, message acknowledged after success |
| Completed runtime | STOPPED, exit 0, extra stop returned HTTP 200 |
| Reported cumulative Claude costs | USD 0.040372 then 0.055494; provider-reported estimates |
| PostgreSQL dump/restore | all 18 tables' row counts matched; 3 ledger transactions; balance sum 0 |
| API and agent images | both built locally; API image includes working Docker CLI and migrations |
| Nginx | syntax valid; certificate-verified local HTTPS; unauthenticated 401 / authenticated 200 |
| Nginx admission | 100-request burst returned 29 x HTTP 200 and 71 x HTTP 429 |
| Compose | base and gateway override parse; default/custom volume options round-trip |
| Experiment UI | queue, history, comparison, details; no root overflow at 375 or 1280 px |
| Ruff / repository hygiene / whitespace | passed |

Pytest emits one upstream Starlette/httpx deprecation warning. It does not skip
or fail any test in the combined run. The real-model smoke uses the configured
Anthropic-compatible provider directly, with the native CLI budget limit.

## Implemented

- Durable experiment queue, independent repeats, stop retries, interrupted-launch
  cleanup without replaying prompts, persisted runtime identities before Docker.
- Host ownership with FileLock or PostgreSQL advisory locks, conditional launch
  claiming, startup/API lifecycle coordination, cross-host mutation rejection.
- Frozen terminal measurements, comparison, provenance, JSON/CSV event exports,
  authenticated Prometheus metrics and experiment UI.
- Bounded employer funding and escrow/refunds; member-only group conversations
  through HTTP and the existing MCP server.
- Viewer/operator/admin permissions; Docker secrets for control-plane tokens.
- Optional LiteLLM virtual keys with model restriction, budget, TTL, hashed storage,
  revocation and cleanup retry; no custom gateway or model loop.
- Docker internal-network, explicit proxy and volume-driver options; fail-closed
  admission for required bounded tmpfs workspaces.
- Explicit archive-retention preview/apply; native consistent SQLite backup/restore;
  standard PostgreSQL and Docker volume recovery instructions.
- Linux PostgreSQL/Nginx TLS deployment reference, optional gateway override,
  Prometheus profile, and CI building the checked-out agent source before tests.

The four review fixes are included: all-agent stop attempts with an archived
partial result, idempotent completed-runtime stop, exact packaged-source image
checks, and explicit documentation that operator messages require MCP reads.

## Acceptance limits

The LiteLLM key API contract is tested with a mock; an actual gateway deployment,
its translation and pricing need external acceptance. Native CLI and gateway
budgets can overshoot and are not provider-account billing ceilings. The reported
Claude cost is cumulative; do not sum both turn totals as separate charges.

The Nginx component was exercised with copied test configuration/certificates.
Docker Desktop bind-mount startup stalled, so the complete Linux Compose topology,
real certificates, firewall/egress ACLs and off-host backup process remain target-
host acceptance work. Persistent disk quota drivers are not certified here;
ordinary volumes remain unbounded, while the tested bounded tmpfs option is volatile.

There is no automatic host failover, automatic wake/rebirth, mandatory notes,
memory injection, or automatic deletion of historical SQL records/workspaces.
Messages to operator channels and groups require explicit MCP reads.

See [OPERATIONS.md](OPERATIONS.md), [EXPERIMENTS.md](EXPERIMENTS.md), and
[TESTING.md](TESTING.md) for the implemented contracts and reproducible commands.
