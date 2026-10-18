"""Role detection in the scripted mock LLM."""

from supervisor.mock_llm import MockLLM


def body(text: str) -> dict:
    return {"messages": [{"role": "user", "content": [{"type": "text", "text": text}]}]}


def test_default_roster():
    mock = MockLLM()
    assert mock._agent(body("You are agent-a. Your peer is agent-b.")) == (
        "agent-a",
        "agent-b",
    )
    assert mock._agent(body("You are agent-b. Your peer is agent-a.")) == (
        "agent-b",
        "agent-a",
    )


def test_custom_roster():
    mock = MockLLM(roster={"alice": "bob", "bob": "alice"})
    assert mock._agent(body("You are alice. Your peer is bob.")) == ("alice", "bob")


def test_peer_quote_does_not_override_genesis():
    mock = MockLLM(roster={"alice": "bob", "bob": "alice"})
    request = {
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "You are alice. Your peer is bob."}],
            },
            {
                "role": "user",
                "content": [{"type": "text", "text": "bob said: You are bob."}],
            },
        ]
    }
    assert mock._agent(request) == ("alice", "bob")
