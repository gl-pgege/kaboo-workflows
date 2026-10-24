"""What ag-ui-strands and strands do on their own, with no kaboo adapter in between.

Each test pins an upstream behaviour kaboo-workflows is designed around. When
one fails after an upgrade, upstream changed: revisit the design note in the
test's docstring rather than updating the assertion. Set ``KABOO_RECORD=1`` to
write each run's events to ``tests/contract/recordings/``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ag_ui.core import ResumeEntry, RunAgentInput, UserMessage
from ag_ui_strands import StrandsAgent, StrandsAgentConfig
from strands import Agent, tool
from strands.session.repository_session_manager import RepositorySessionManager
from strands.session.session_repository import SessionRepository
from strands.types.session import Session, SessionAgent, SessionMessage
from strands.types.tools import ToolContext

from kaboo_workflows.testing import ScriptedModel
from kaboo_workflows.tools.wrappers import node_as_async_tool

RECORDINGS = Path(__file__).parent / "recordings"
ANSWERS: list[Any] = []


@tool(context=True)
def delete_widget(widget_id: str, tool_context: ToolContext) -> str:
    """Delete a widget from a dashboard.

    Args:
        widget_id: The widget to delete.
    """
    answer = tool_context.interrupt(
        "kaboo:approval", reason={"tool": "delete_widget", "widget_id": widget_id}
    )
    ANSWERS.append(answer)
    if isinstance(answer, dict) and answer.get("cancelled"):
        return "The person skipped this."
    return f"Deleted {widget_id}."


DELETE_THEN_REPLY = [
    [{"tool": "delete_widget", "input": {"widget_id": "w_2"}}, {"text": "Deleted."}],
    [{"text": "MRR is monthly recurring revenue."}],
]


def _agent(turns: list[list[dict[str, Any]]], **kwargs: Any) -> Agent:
    return Agent(
        model=ScriptedModel(turns=turns), tools=[delete_widget], callback_handler=None, **kwargs
    )


def _input(run_id: str, messages: list[Any], **extra: Any) -> RunAgentInput:
    return RunAgentInput(
        thread_id="contract",
        run_id=run_id,
        state={},
        messages=messages,
        tools=[],
        context=[],
        forwarded_props={},
        **extra,
    )


async def _run(agui: StrandsAgent, input_data: RunAgentInput, name: str) -> list[dict[str, Any]]:
    events = [
        event.model_dump(by_alias=True, exclude_none=True, mode="json")
        async for event in agui.run(input_data)
    ]
    if os.environ.get("KABOO_RECORD"):
        RECORDINGS.mkdir(exist_ok=True)
        (RECORDINGS / f"{name}.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in events)
        )
    return events


def _snapshot(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if event["type"] == "MESSAGES_SNAPSHOT"][-1]["messages"]


def _text(events: list[dict[str, Any]]) -> str:
    return "".join(
        event.get("delta", "") for event in events if event["type"] == "TEXT_MESSAGE_CONTENT"
    )


def _paused_on(events: list[dict[str, Any]]) -> dict[str, Any]:
    outcome = events[-1]["outcome"]
    assert outcome["type"] == "interrupt"
    [interrupt] = outcome["interrupts"]
    return interrupt


async def test_a_tool_interrupt_publishes_its_name_and_reason_but_no_tool_call() -> None:
    """A tool's own interrupt reaches the client without the call it paused.

    Design: the approval card cannot find its tool row from upstream's fields,
    so kaboo-workflows lifts ``toolCallId``, ``subagentRunId``, ``expiresAt``
    and ``responseSchema`` out of the reason it raises with.
    """
    events = await _run(
        StrandsAgent(agent=_agent(DELETE_THEN_REPLY), name="lab"),
        _input("r1", [UserMessage(id="u1", role="user", content="delete w_2")]),
        "tool-interrupt",
    )
    interrupt = _paused_on(events)
    assert interrupt["reason"] == "kaboo:approval"
    assert interrupt["metadata"] == {"reason": {"tool": "delete_widget", "widget_id": "w_2"}}
    assert "toolCallId" not in interrupt
    assert "responseSchema" not in interrupt


async def test_a_delegates_approval_arrives_as_the_delegate_call_with_an_opaque_reason() -> None:
    """Spike 1: a delegate's approval carries neither its own call nor its sub-agent.

    Only the delegate tool's call is on the wire, and the interrupt's reason is
    the child's interrupt id. The answer still reaches the child's tool.
    """
    ANSWERS.clear()
    child = _agent(
        [[{"tool": "delete_widget", "input": {"widget_id": "w_2"}}, {"text": "Deleted w_2."}]],
        name="visualizer",
        agent_id="visualizer",
    )
    parent = Agent(
        model=ScriptedModel(
            turns=[[{"tool": "visualizer", "input": "Delete w_2"}, {"text": "Done."}]]
        ),
        tools=[node_as_async_tool(child, name="visualizer", description="Edits dashboards.")],
        callback_handler=None,
    )
    agui = StrandsAgent(agent=parent, name="lab")
    first = await _run(
        agui, _input("r1", [UserMessage(id="u1", role="user", content="delete w_2")]), "delegate-r1"
    )

    assert [event["toolCallName"] for event in first if event["type"] == "TOOL_CALL_START"] == [
        "visualizer"
    ]
    interrupt = _paused_on(first)
    assert interrupt["reason"].startswith("v1:tool_call:")
    assert "toolCallId" not in interrupt and "subagentRunId" not in interrupt

    approved = ResumeEntry(
        interrupt_id=interrupt["id"], status="resolved", payload={"approved": True}
    )
    second = await _run(agui, _input("r2", _snapshot(first), resume=[approved]), "delegate-r2")
    assert ANSWERS == [{"response": {"approved": True}}]
    assert _text(second) == "Done."


async def test_a_cancelled_resume_ignores_a_new_message_sent_with_it() -> None:
    """Spike 2: skipping an approval and asking something new must be two runs.

    The cancelled resume is accepted and the paused tool sees the cancellation,
    but the new message never reaches the model: the old turn finishes instead,
    and the snapshot files the new message before the skipped call's result.
    """
    ANSWERS.clear()
    agui = StrandsAgent(agent=_agent(DELETE_THEN_REPLY), name="lab")
    first = await _run(
        agui, _input("r1", [UserMessage(id="u1", role="user", content="delete w_2")]), "skip-r1"
    )
    skip = ResumeEntry(interrupt_id=_paused_on(first)["id"], status="cancelled")
    messages = [*_snapshot(first), {"id": "u2", "role": "user", "content": "What is MRR?"}]
    second = await _run(agui, _input("r2", messages, resume=[skip]), "skip-r2")

    assert ANSWERS == [{"cancelled": True}]
    assert second[-1]["outcome"] == {"type": "success"}
    assert _text(second) == "Deleted."
    roles = [message["role"] for message in _snapshot(second)]
    assert roles == ["user", "assistant", "user", "tool", "assistant"]


class StateRepository(SessionRepository):
    """Every session record in one JSON-safe dict, the way a state key would hold it."""

    def __init__(self, store: dict[str, Any]) -> None:
        self.store = store

    def _session(self, session_id: str) -> dict[str, Any]:
        return self.store.setdefault(
            session_id, {"session": None, "agents": {}, "multi_agents": {}}
        )

    def create_session(self, session: Session, **kwargs: Any) -> Session:
        self._session(session.session_id)["session"] = session.to_dict()
        return session

    def read_session(self, session_id: str, **kwargs: Any) -> Session | None:
        data = self.store.get(session_id, {}).get("session")
        return Session.from_dict(data) if data else None

    def create_agent(self, session_id: str, session_agent: SessionAgent, **kwargs: Any) -> None:
        self._session(session_id)["agents"][session_agent.agent_id] = {
            "agent": session_agent.to_dict(),
            "messages": [],
        }

    def read_agent(self, session_id: str, agent_id: str, **kwargs: Any) -> SessionAgent | None:
        entry = self.store.get(session_id, {}).get("agents", {}).get(agent_id)
        return SessionAgent.from_dict(entry["agent"]) if entry else None

    def update_agent(self, session_id: str, session_agent: SessionAgent, **kwargs: Any) -> None:
        self._session(session_id)["agents"][session_agent.agent_id]["agent"] = (
            session_agent.to_dict()
        )

    def create_message(
        self, session_id: str, agent_id: str, session_message: SessionMessage, **kwargs: Any
    ) -> None:
        self._session(session_id)["agents"][agent_id]["messages"].append(session_message.to_dict())

    def read_message(
        self, session_id: str, agent_id: str, message_id: int, **kwargs: Any
    ) -> SessionMessage | None:
        for data in self._session(session_id)["agents"][agent_id]["messages"]:
            if data["message_id"] == message_id:
                return SessionMessage.from_dict(data)
        return None

    def update_message(
        self, session_id: str, agent_id: str, session_message: SessionMessage, **kwargs: Any
    ) -> None:
        messages = self._session(session_id)["agents"][agent_id]["messages"]
        for index, data in enumerate(messages):
            if data["message_id"] == session_message.message_id:
                messages[index] = session_message.to_dict()

    def list_messages(
        self,
        session_id: str,
        agent_id: str,
        limit: int | None = None,
        offset: int = 0,
        **kwargs: Any,
    ) -> list[SessionMessage]:
        messages = self._session(session_id)["agents"][agent_id]["messages"][offset:]
        return [
            SessionMessage.from_dict(data) for data in (messages[:limit] if limit else messages)
        ]

    def create_multi_agent(self, session_id: str, multi_agent: Any, **kwargs: Any) -> None:
        self._session(session_id)["multi_agents"][multi_agent.id] = multi_agent.serialize_state()

    def read_multi_agent(
        self, session_id: str, multi_agent_id: str, **kwargs: Any
    ) -> dict[str, Any] | None:
        return self.store.get(session_id, {}).get("multi_agents", {}).get(multi_agent_id)

    def update_multi_agent(self, session_id: str, multi_agent: Any, **kwargs: Any) -> None:
        self._session(session_id)["multi_agents"][multi_agent.id] = multi_agent.serialize_state()


async def test_a_repository_over_state_resumes_a_paused_agent_in_a_fresh_process() -> None:
    """Spike 3: a session repository over a JSON state value rebuilds a paused agent.

    Run 2 uses a new adapter and a new agent, so nothing is cached: the paused
    call, its interrupt and the conversation come back from the store alone.
    """
    ANSWERS.clear()
    store: dict[str, Any] = {}

    def fresh() -> StrandsAgent:
        return StrandsAgent(
            agent=_agent(DELETE_THEN_REPLY),
            name="lab",
            config=StrandsAgentConfig(
                session_manager_provider=lambda input_data: RepositorySessionManager(
                    session_id=input_data.thread_id, session_repository=StateRepository(store)
                )
            ),
        )

    first = await _run(
        fresh(),
        _input("r1", [UserMessage(id="u1", role="user", content="delete w_2")]),
        "session-r1",
    )
    persisted = json.loads(json.dumps(store))
    store.clear()
    store.update(persisted)

    approved = ResumeEntry(
        interrupt_id=_paused_on(first)["id"], status="resolved", payload={"approved": True}
    )
    second = await _run(fresh(), _input("r2", _snapshot(first), resume=[approved]), "session-r2")
    assert ANSWERS == [{"response": {"approved": True}}]
    assert second[-1]["outcome"] == {"type": "success"}
    assert _text(second) == "Deleted."
