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

Death is terminal. Messages never start or resume stopped/suspended runtimes.
There are no mandatory notes, memory injections, wake events, or rebirth.

## Remaining work

Cross-experiment metrics/comparison, durable multi-worker scheduling, named
employer budgets, network/storage quotas, and production deployment remain
separate milestones.
