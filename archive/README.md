# Archive

This directory contains frozen prototypes and historical references. Nothing here is imported by, built by, or required to run the current Tsinghua City world service.

## genesis-lab

`genesis-lab/` is the earlier Docker/Claude-Code two/N-agent chat prototype. It contains a working message-delivery and container-orchestration prototype, but it is **not** the authority for world state and is not integrated with `tsinghua_city/`.

It is retained because a few pieces are useful references for future phases:

- `genesis-lab/runtime/bridge.py` — safe-point delivery state machine
- `genesis-lab/runtime/inbox.py`, `harness.py`, `formatting.py` — delivery/harness integration
- `genesis-lab/supervisor/compose.py`, `dockerctl.py` — container-runtime reference
- `genesis-lab/supervisor/webconsole.py` — UI reference for a future Canvas
- `genesis-lab/tests/` — delivery/runtime test scenarios worth porting if the safe-point path is revived

It should not be used as a library, and its old `DESIGN.md` is not the canonical design. The canonical design is [../Design.md](../Design.md); current status is [../docs/STATUS.md](../docs/STATUS.md).

The current system has **zero direct imports or test imports from this archive**.
