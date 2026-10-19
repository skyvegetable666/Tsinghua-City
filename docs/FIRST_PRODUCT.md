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
2. Add a thin Claude harness:
   - use the official Claude Agent SDK
   - connect to the existing MCP server
   - no custom browser, task, or policy loop
3. Add a thin Docker runtime adapter:
   - non-root container
   - read-only application code
   - private workspace volume
   - explicit token and secret injection
4. Add a run configuration:
   - model
   - system prompt
   - timeout and max turns
   - allowed tools
   - workspace and MCP environment
5. Capture run transcripts:
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

## 6. Current blockers

- Docker Desktop is installed on the Windows host, but WSL integration is not
  enabled, so the Docker CLI is unavailable inside this workspace.
- The Claude Code CLI is installed and reports version 2.1.296; model
  authentication and the Python Agent SDK dependency still need verification.
- The current runtime has no safe secret or model-credential injection path.
- The local runtime adapter is a development convenience, not a sandbox.

## 7. Not in the first product

- public mutable workspace and its locking model
- agent lives, wake, rebirth, or mandatory diaries
- periodic credit decay or a separate bank institution
- advanced market mechanisms
- a full experiment comparison UI
- multi-worker durable scheduling

Those are later slices, and none of them should delay the real harness and
sandbox path.
