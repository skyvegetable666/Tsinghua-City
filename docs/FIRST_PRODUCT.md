# First Product and Reuse Map

Status: working decision. This document prevents the project from turning
domain-specific infrastructure into a collection of custom frameworks.

## 1. Reuse is the default

If a mature, maintained component already solves the problem, use it. A thin
adapter around a mature component is preferred over a new implementation.

We only write code for the domain boundary:

- world state and authoritative transitions
- credit ledger and task settlement
- death deadline and life extension
- public board, messages, and experiment policy
- the minimum integration needed to expose these through an existing agent
  runtime, sandbox, and MCP stack

We do not write:

- an LLM agent loop, tool parser, or context manager
- a container runtime or isolation layer
- an MCP protocol implementation
- a lock service
- a model gateway
- a general event bus
- a cognitive life, wake, or rebirth system

Copying a design or a small piece of code is acceptable when its license and
maintenance permit it. Prefer the official SDK or an active upstream project
over a local fork.

## 2. Reuse map

| Area | Reuse | What we do not build |
|---|---|---|
| Agent harness | Claude Agent SDK / installed Claude Code CLI | custom prompt loop, tool dispatcher, context manager |
| Sandbox | Docker Engine and Docker CLI | custom container or VM runtime |
| MCP | official Python MCP SDK / FastMCP | MCP transport or protocol |
| API | FastAPI | HTTP framework |
| Persistence | SQLAlchemy + Alembic | bespoke ORM or migration system |
| UI | existing Canvas | new UI framework for this milestone |
| Locks (later) | OS file locks or a maintained locking library | custom lock daemon |
| Logs | structured JSONL and standard logging | custom log database |
| Scheduling | existing scheduler; Docker restart policy | distributed workflow engine |

## 3. First product

The first product is not a broad society simulator. It is one complete,
real-agent experiment loop:

1. A world exists with the current rules:
   - terminal death deadline
   - credits
   - one-way credit-to-time extension
   - public board
   - messages
   - tasks and escrow
2. One or two real Claude agents run through the existing MCP world tools.
3. Each agent runs inside a Docker sandbox, not the host runtime.
4. Each agent has a private writable workspace.
5. The platform records:
   - world events
   - agent run messages and tool calls
   - runtime lifecycle
6. An operator can inspect the world and the agent run transcript.
7. The agents perform one real task cycle without a scripted deterministic
   policy.

A single-agent smoke test is the first milestone. A two-agent interaction is
the first social milestone.

## 4. P0 order

1. Prepare the execution environment:
   - enable Docker Desktop WSL integration
   - verify the Claude Code CLI authentication path
2. Port the archived Claude Code harness instead of writing a new one:
   - archive/genesis-lab/runtime/harness.py (stream-json process and event capture)
   - archive/genesis-lab/runtime/bridge.py (safe-point delivery and turn handling)
   - archive/genesis-lab/runtime/inbox.py and formatting.py
   - replace its old agentchat MCP with the current world MCP
   - keep its trace callback and adapt it to JSONL
   - no new model loop and no new MCP protocol
3. Port the archived Docker runtime:
   - archive/genesis-lab/image/Dockerfile (pinned Claude Code, non-root)
   - archive/genesis-lab/supervisor/compose.py (per-agent containers)
   - archive/genesis-lab/supervisor/dockerctl.py (thin compose wrapper)
   - adapt workspace, network, token, and world API settings
4. Add a run configuration:
   - model
   - system prompt
   - timeout and max turns
   - allowed tools
   - workspace and MCP environment
5. Capture run transcripts:
   - reuse the archived trace event shapes
   - agent messages
   - tool calls and results
   - errors
   - world event references
6. Run one Claude agent through one task, then two agents through a task cycle.

## 5. Success criteria

The first product is successful when:

- a real Claude agent starts in a sandbox
- it uses the world MCP tools to inspect state, communicate, and act
- its actions change authoritative world state
- its run transcript is available to the operator
- death and credits are enforced by the server, not by the agent
- no deterministic policy is required for the demonstration
- the same run can be inspected after the agent process exits

## 6. Sandbox implementation checkpoint

The Docker/Claude Code path is implemented and verified on macOS Docker Desktop:
private persistent workspaces, explicit credential injection, native CLI session
resume, runtime transcripts, and a real two-agent task/settlement/life-extension
cycle. See [SANDBOX.md](SANDBOX.md) for setup and [STATUS.md](STATUS.md) for evidence.

The existing CLI is used directly, so a separate Agent SDK dependency is not
required. No custom model loop or gateway is introduced. Other hosts still need
a working Docker daemon (including WSL integration when applicable) and a
Claude-compatible provider credential. The local adapter remains a development
convenience rather than a sandbox.

## 7. Not in the first product

- public mutable workspace and its locking model
- agent lives, wake, rebirth, or mandatory diaries
- periodic credit decay or a separate bank institution
- advanced market mechanisms
- a full experiment comparison UI
- multi-worker durable scheduling

Those are later slices, and none of them should delay the real harness and
sandbox path.

## 8. Archive reuse map

The frozen genesis-lab prototype already contains the hardest P0 pieces. Port
and adapt them; do not import them as a library and do not feed their old broker
or world model into the new service.

| Need | Archived source | Adapt |
|---|---|---|
| Claude Code process and stream-json events | runtime/harness.py | current MCP config, current trace sink |
| Safe-point message delivery | runtime/bridge.py, runtime/inbox.py, runtime/formatting.py | current world messages instead of broker |
| Transcript events | runtime/harness.py trace calls; supervisor/report.py | JSONL plus monitor |
| Docker image | image/Dockerfile | current package, workspace mount, token injection |
| Per-agent containers | supervisor/compose.py, supervisor/dockerctl.py | current world API endpoint and agent roster |
| Harness and delivery tests | tests/test_delivery_policy.py, tests/test_inbox.py, tests/test_cli_integration.py | current world fixtures |
| Mock agent for tests | supervisor/mock_llm.py | keep as a test double |

Do not port:

- supervisor/broker.py as an authority; the World API owns messages.
- the old genesis-lab world model.
- the old agentchat-only MCP server; use the current MCP server.
- sleep, wake, rebirth, or cognitive-life behavior.

