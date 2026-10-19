# Testing guide

- **Integrated upstream base:** `4824d6b`
- **Default expectation:** `74 passed, 9 skipped` (seven Docker + two Postgres checks)
- **With Docker:** `81 passed, 2 skipped`

## 1. Local setup

~~~bash
cd Tsinghua-City
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

pytest -q
ruff check tsinghua_city tests migrations
python scripts/check_hygiene.py
~~~

Requirements: Python 3.11+; current test environment uses 3.12/3.13.

Real Docker boundary and MCP tests:

```sh
docker build -f deploy/agent.Dockerfile -t tsinghua-city-agent:0.1 .
CITY_TEST_DOCKER=1 pytest -q
```

See [SANDBOX.md](SANDBOX.md) for what these checks cover and for the separate
real-model setup; the Docker tests themselves make no paid model requests.

## 2. Windows system proxy

The test client and MCP sidecar intentionally bypass system proxies for loopback:

- `tests/test_mcp_protocol.py` uses `httpx.Client(trust_env=False)`
- `tsinghua_city/mcp_server.py` uses an empty `ProxyHandler`

Still set this for external harnesses:

~~~powershell
$env:NO_PROXY = "127.0.0.1,localhost"
~~~

Run without `NO_PROXY` to verify the loopback test:

~~~powershell
pytest -q tests/test_mcp_protocol.py
# 1 passed
~~~

## 3. Postgres concurrency

Use a **disposable** database. The fixture drops and recreates tables.

~~~bash
pip install -e '.[dev,postgres]'
export CITY_TEST_DATABASE_URL=postgresql+psycopg://postgres:city@127.0.0.1:5432/city_test
pytest -q
# expected without Docker: 76 passed, 7 skipped

pytest -q tests/test_postgres_concurrency.py -vv
# both tests must pass; one transfer is 409, the other ok
~~~

The double-spend regression is:

- two concurrent transfers of 80 from a 100-credit balance
- expected result set: `['409', 'ok']`
- not `['ok', 'ok']`

## 4. Reference harness / demo

The deterministic harness is covered by policy/decoder tests:

~~~bash
pytest -q tests/test_harness_policy.py tests/test_reference_harness.py
# 4 passed
~~~

Manual demo (requires a running API and the same venv):

~~~bash
CITY_ADMIN_TOKEN=change-me \
python scripts/two_agent_demo.py --ticks 6 --interval 1
~~~

Expected: alice/bob publish and complete tasks; JSONL journals appear under
`runs/demo-journals/<world>/`. The harness exits after its ticks, so a later
GET reports a successful runtime as STOPPED with exit_code 0; it must not restart.

### Claude harness (fake CLI)

~~~bash
pytest -q tests/test_claude_harness.py tests/test_claude_agent.py
# 11 passed
~~~

The tests start a fake Claude CLI using the stream-json protocol. They verify
that the harness sends the first user turn, captures init/assistant/result
events, constructs an MCP config command, and writes the agent entrypoint's
JSONL trace. They do not require a model, credentials, or Docker.

They also check bounded continuation, message acknowledgement after successful
replies, failed-reply retention, CLI budget arguments, native limit results, and
wall-time exhaustion. `tests/test_experiments.py` checks manifest validation,
batch start/stop, private condition archives, and partial-launch cleanup.

## 5. Operator channel

~~~bash
pytest -q tests/test_operator.py
# 2 passed
~~~

Admin sends through
`POST /v1/worlds/{world}/agents/{agent}/operator/messages`.
Agents read with `GET /v1/me/operator/messages`, reply with
`POST /v1/me/operator/messages`, and mark read with
`POST /v1/me/operator/messages/read`. MCP exposes
`get_operator_messages`, `talk_to_operator`, and
`mark_operator_messages_read`.

## 6. Run manifests

~~~bash
pytest -q tests/test_manifests.py
# 1 passed
~~~

Each runtime start writes a secret-free JSON manifest under
`CITY_MANIFEST_DIR` (default `runs/manifests`). Admin can list an agent's
manifests and read one by name. Commands, adapter, image, limits, trace/log
paths, and timestamps are included; credentials are not.

## 7. Trace viewer

~~~bash
pytest -q tests/test_traces.py
# 3 passed
~~~

The server lists JSONL files under `CITY_TRACE_DIR` (default
`runs/transcripts`) and serves them at `/traces`. The viewer is read-only and
uses the admin token. Docker stop/crash cleanup copies the container's
`/work/transcripts/*.jsonl` into the host trace directory. Structured stdout is
also exported as a per-launch live snapshot during maintenance polls.

## 8. Docker image smoke

~~~bash
scripts/docker_image_smoke.sh
~~~

Builds `tsinghua-city-agent:0.1` and checks the non-root UID, writable
`/work` and `/shared`, `flock`, Claude Code, and the Python package. It does
not require model credentials or start a world.

## 9. Live Claude Code smoke

~~~bash
export ANTHROPIC_AUTH_TOKEN=...
# or ANTHROPIC_API_KEY=...
python scripts/live_claude_smoke.py
~~~

Requires a working Docker CLI and the built agent image. The script starts a
disposable world/API, launches the Docker sandbox, asks Claude Code to call
`check_state` once, and asserts `LIVE_CLAUDE_OK` plus a tool result. It never
prints credentials. In this environment Docker Desktop's Windows CLI was
bridged because WSL integration was unavailable.

## 10. Doctor

~~~bash
tsinghua-city doctor
~~~

Checks the database migration path, runtime/trace/manifest directories,
\`CITY_ADMIN_TOKEN\`, Docker CLI/daemon/image, Claude CLI, and model
credentials. It exits non-zero when Docker or Claude is missing; this is the
fastest way to see why a real run cannot start.

## 11. Canvas smoke

Open `http://127.0.0.1:8000/canvas` or:

~~~bash
pytest -q tests/test_canvas.py
# 2 passed
~~~

Enter the admin token, select a world, and verify agents/tasks/posts/events/runtime
statuses render. The Canvas is an admin monitor; it does not expose agent tokens.

## 12. MCP protocol

~~~powershell
pytest -q tests/test_mcp_protocol.py -s
~~~

This starts a live World API and a real `python -m tsinghua_city mcp` stdio process, then checks `tools/list` and `tools/call`.

## 13. Runtime manual acceptance

Create a world and at least two agents.

Start a runtime with the admin token:

~~~json
{
  "command": ["<python>", "-c", "import time; time.sleep(60)"],
  "restart_policy": "on_failure",
  "max_restarts": 2
}
~~~

Check:

- status `RUNNING`
- `pid`, `log_path`, `mcp_config_path` exist
- old agent token returns 401
- agent process environment does not contain `CITY_ADMIN_TOKEN`
- suspend stops the process, status `SUSPENDED`
- send a message and run `tsinghua-city tick`: runtime stays suspended; explicitly resume it
- stop removes the process and MCP config; log remains
- kill the process, then `GET /runtime` or reconcile marks `CRASHED`
- with `on_failure`, wait for `next_restart_at` / tick and verify a new pid, `restart_count = 1`, and a changed MCP config token
- the old token from before restart must return 401; the new MCP config token must work
- exceed `max_restarts`: runtime stays `CRASHED`
- `terminate` a crashed runtime: status becomes `STOPPED` and no restart occurs

If `serve` is running, its scheduler may restart crashed processes before manual `tick`. For deterministic manual tests, stop the server or set `CITY_SCHEDULER_ENABLED=0`.

## 14. Migration checks

Fresh database:

~~~bash
tsinghua-city init
# alembic_version = 0006_operator_messages
~~~

Incremental upgrade from an older checkpoint:

~~~bash
tsinghua-city upgrade
# verify existing worlds/agents/messages remain
# verify new columns/tables exist
~~~

Current tables include: worlds, agents, accounts, ledger transactions/entries, messages, tasks, posts, post_replies, agent_runtimes, events.

## 15. API smoke

Start:

~~~bash
CITY_ADMIN_TOKEN=change-me tsinghua-city serve
~~~

Open `http://127.0.0.1:8000/docs`.

Useful manual flows:

- create world and two agents
- transfer, message, task, board
- start/suspend/resume/stop a runtime
- inspect events with admin token

## 16. Reporting

Include:

- Windows/Linux/macOS and Python version
- commit hash
- proxy state
- Postgres availability
- exact command
- expected vs actual
- exit code and full traceback

Never include agent tokens, admin tokens, or full MCP config files in reports.
