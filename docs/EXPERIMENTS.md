# Bounded Docker experiments

Use the existing World API, Docker adapter, and Claude Code CLI. No model loop,
broker, memory policy, or automatic runtime activation is added.

## Configure and launch

1. Start the World API using the setup in [SANDBOX.md](SANDBOX.md).
2. Copy `examples/two-agent.json`, select a Claude-compatible model for each
   agent, and review prompts, world parameters, and limits.
3. Validate without starting containers or making model calls:

   ```sh
   tsinghua-city run-experiment examples/two-agent.json --dry-run
   ```

4. Launch against the running API using the operator's existing environment:

   ```sh
   tsinghua-city run-experiment examples/two-agent.json
   ```

All peers are created before any runtime starts. Runtimes use `adapter: docker`
and `restart_policy: never`. A launch failure stops this launch's new runtimes;
the failed world remains available for inspection. Cleanup failures are recorded
as `cleanup_pending` in `run.json`.

Each launch archives a normalized version-1 `manifest.json` and a `run.json`
containing experiment/world/agent/runtime IDs under
`runs/experiments/<experiment_id>/`. Provider keys and agent tokens are not
written to the archive. Credentials remain in the existing server environment.
The manifest captures conditions, not deterministic model results or a pinned
snapshot of every dependency.

Stop all runtimes belonging to an experiment:

```sh
tsinghua-city stop-experiment runs/experiments/<experiment_id>/run.json
```

This stops execution; real-world life deadlines continue to advance.
Stopping is best-effort across all agents: a failed stop never prevents later
agents from being stopped. The CLI persists each outcome in `run.json`, returns
exit code 1 with status `cleanup_pending` on partial failure, and clears errors
after a successful retry. Stopping an already stopped runtime is an HTTP 200 no-op.

## Limits and message delivery

- `max_turns`: native Claude tool/model-turn limit.
- `max_budget_usd`: native Claude Code `--max-budget-usd`, per agent session.
- `max_rounds`: maximum user turns, including the initial prompt.
- `run_timeout`: total time including startup, model calls, and inbox waiting.
- `turn_timeout`: maximum time waiting for a single result.
- `listen_seconds`: maximum idle wait for messages after each completed turn.
- `poll_interval`: interval between checks while waiting for messages.

The native CLI keeps ownership of tool execution, context, and model calls.
Between completed user turns, an already running harness checks its World API
inbox and delivers unread messages with their sender and ID. Delivery follows
the archived inbox/formatting design. It never writes into an active turn and
never manufactures a continuation prompt. Messages are marked read only after
the corresponding model turn succeeds; failed delivery remains unread.

The turn-boundary inbox polls only peer messages at `/v1/me/messages`.
Operator messages at `/v1/me/operator/messages` require an explicit MCP read;
they are not injected into continuation turns. Group conversation messages also
require explicit MCP reads. None of these messages start or resume a runtime.

Manual agent invocations still default to one user turn. Multiple rounds require
an explicit positive spending limit. Time, turn, or budget exhaustion ends a run;
successful completion is `STOPPED`, not `CRASHED`, and is never auto-restarted.
Provider errors remain failures. Death stops execution on the next maintenance
sweep, or immediately when an operator queries that runtime's status. If Docker
is unavailable, cleanup retries on subsequent sweeps without permitting rebirth.

CLI dollar limits depend on the provider's reported/priced usage and may overshoot
by an in-flight request. They are not provider-account billing caps, do not limit
arbitrary direct requests made with provider credentials, and are separate from
world credits. Use provider-side quotas for a strict account spending boundary.

## Inspect

Canvas agent profiles link to traces filtered by world and agent. Docker exports
structured stdout events to the host trace directory during maintenance polls
and stop, while keeping raw stdout/stderr in the runtime log. Each launch has its
own trace file. `/v1/traces` accepts world, agent, run, and experiment ID filters;
trace reads support `offset` and `limit`. The viewer supports refresh and paging.
Upstream's full `/work/transcripts` copy on stop is retained alongside these
per-launch stdout snapshots, so full transcript archives remain available too.

Death is terminal. Messages never start or resume stopped/suspended runtimes.
There are no mandatory notes, memory injections, wake events, or rebirth.

## Durable experiment operations

Open `/experiments` to edit a manifest, queue independent trials, inspect history,
compare measurements, and download JSON/CSV results. `POST /v1/experiments`
accepts `{"manifest": {...}, "repeat": 3}` and returns HTTP 202 with persisted
`QUEUED` runs. A batch contains at most 100 agent runtimes. The legacy CLI above
launches immediately and keeps a local archive; it does not enqueue registry runs.

The host maintenance loop claims each queued run once, creates all peer identities,
then launches their containers. Every runtime identity is persisted before Docker
starts. An interrupted `STARTING` run is cleaned up and becomes `FAILED`; its
prompts are never replayed automatically. Stop requests enter `STOPPING` and retry
cleanup for every agent until complete. A failed agent stops the remaining peers.
`COMPLETED`, `FAILED`, and `STOPPED` are terminal experiment states. Independent
trials create new worlds and identities; they do not resurrect dead agents.

SQLite uses FileLock on one host. PostgreSQL uses a session advisory lock per
`CITY_RUNTIME_HOST_ID`, plus a conditional database update for run claiming.
Startup reconciliation and API lifecycle mutations use the same host lock;
overlapping API actions return 409 with a retry message. A stable, unique host
ID owns each run/runtime. Other hosts may inspect it and request a durable stop,
but cannot manipulate its process. Legacy runtimes with no owner are compatible
with a single-host upgrade; migrate/adopt those before introducing multiple hosts.
This is host ownership, not failover: a host outage requires explicit operator
recovery on that host. Multiple SQLite hosts are unsupported.

Results include task completion/status, peer-message counts/read receipts,
survival time, credit totals/distribution/Gini, tool calls, and reported model cost.
Each terminal run stores a metric snapshot, so later activity in its world does
not rewrite historical results. The export includes ordered world events through
the terminal timestamp. Tool-use IDs deduplicate stdout/transcript copies; cost
uses the maximum cumulative Claude session total per run ID. Missing price/cost
reporting is `null`, not zero. Scan truncation is explicit. Operator/group-message
counts are not included in peer-message metrics. These observational metrics do
not prove causal effects; use repeated trials and inspect individual traces.

Provenance includes the Git commit, dirty status, SHA-256 of the packaged working
source, configured agent image and inspected image ID. It records uncommitted
code honestly, but does not claim dependency/model determinism. Real Docker tests
compare the source inside the image to the working tree and fail with an explicit
rebuild command on mismatch. CI builds a fresh image for the checked-out commit.

See [OPERATIONS.md](OPERATIONS.md) for roles, bounded employers, conversations,
external gateway budgets, resource-policy limits, metrics, retention, and backup.
