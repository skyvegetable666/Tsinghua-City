# Testing guide

- **Code checkpoint:** `9e4c066`
- **Default expectation:** `29 passed, 2 skipped`
- **Postgres expectation:** `31 passed`

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
# expected: 31 passed

pytest -q tests/test_postgres_concurrency.py -vv
# both tests must pass; one transfer is 409, the other ok
~~~

The double-spend regression is:

- two concurrent transfers of 80 from a 100-credit balance
- expected result set: `['409', 'ok']`
- not `['ok', 'ok']`

## 4. MCP protocol

~~~powershell
pytest -q tests/test_mcp_protocol.py -s
~~~

This starts a live World API and a real `python -m tsinghua_city mcp` stdio process, then checks `tools/list` and `tools/call`.

## 5. Runtime manual acceptance

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
- send a message to the agent, wait for scheduler or run `tsinghua-city tick`, runtime wakes
- stop removes the process and MCP config; log remains
- kill the process, then `GET /runtime` or reconcile marks `CRASHED`
- with `on_failure`, wait for `next_restart_at` / tick and verify a new pid, `restart_count = 1`, and a changed MCP config token
- the old token from before restart must return 401; the new MCP config token must work
- exceed `max_restarts`: runtime stays `CRASHED`
- `terminate` a crashed runtime: status becomes `STOPPED` and no restart occurs

If `serve` is running, its scheduler may wake/restart before manual `tick`. For deterministic manual tests, stop the server or set `CITY_SCHEDULER_ENABLED=0`.

## 6. Migration checks

Fresh database:

~~~bash
tsinghua-city init
# alembic_version = 0005_runtime_restart
~~~

Incremental upgrade from an older checkpoint:

~~~bash
tsinghua-city upgrade
# verify existing worlds/agents/messages remain
# verify new columns/tables exist
~~~

Current tables include: worlds, agents, accounts, ledger transactions/entries, messages, tasks, posts, post_replies, agent_runtimes, events.

## 7. API smoke

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

## 8. Reporting

Include:

- Windows/Linux/macOS and Python version
- commit hash
- proxy state
- Postgres availability
- exact command
- expected vs actual
- exit code and full traceback

Never include agent tokens, admin tokens, or full MCP config files in reports.
