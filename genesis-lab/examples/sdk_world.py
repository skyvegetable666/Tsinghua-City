"""Minimal Genesis SDK example.

Run from the repository root with Docker available:

    python examples/sdk_world.py

This uses mock mode, so it needs no API key.  It starts the real supervisor,
waits for genesis, injects one operator message, waits for the run to end, then
writes the transcript/metrics report.
"""

from __future__ import annotations

from genesis import World


def main() -> None:
    with World.create(
        agents=["agent-a", "agent-b"],
        mission="介绍一下你自己，然后和对方交换一个你关心的开放问题。",
        mock=True,
        max_seconds=45,
        max_messages=4,
    ) as world:
        print(f"run={world.run_id}")
        print(f"run_dir={world.run_dir}")
        print(f"broker={world.base_url}")
        print(f"status={world.status()}")
        world.send_system("operator: 实验开始。")
        for event in world.events(follow=True):
            print(event.get("event", "?"))

    metrics = world.report()
    print(f"chat={metrics['messages']['chat']} delivered={metrics['messages']['delivered']}")
    print(f"transcript={world.run_dir / 'transcript.md'}")


if __name__ == "__main__":
    main()
