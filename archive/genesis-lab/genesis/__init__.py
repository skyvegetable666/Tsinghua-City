"""Genesis Lab control-plane SDK.

Quick start (from the repository root):

    from genesis import World

    with World.create(agents=["agent-a", "agent-b"], mock=True) as world:
        print(world.status())
        world.send_system("operator: hello")
        metrics = world.report()

The SDK only orchestrates the existing supervisor/broker/container runtime.
It is not an agent harness and it does not add a second message channel.
"""

from .spec import MIN_AGENTS, AgentSpec, WorldSpec
from .world import World, WorldError

__all__ = [
    "MIN_AGENTS",
    "AgentSpec",
    "World",
    "WorldError",
    "WorldSpec",
]

__version__ = "0.4.0"
