"""Working views reach the model as a base that stays put and a tail of changes.

The base is rendered once per invocation and opens the latest human message, so
a prompt cache over the conversation keeps applying across the invocation's
model calls. What changed since the base ends the latest message at every call.
Only the copy sent to the model changes; the agent keeps its own messages.
"""

from __future__ import annotations

from typing import Any

from strands import Agent, tool

from kaboo_workflows._context import StateChannel, set_activity_context, set_state_channel
from kaboo_workflows.config.resolvers.agents import build_agent_from_def
from kaboo_workflows.hooks import ContextHook, View, ViewRequest
from kaboo_workflows.testing import ScriptedModel
from tests.factories import agent_def


@tool
def lookup(n: int) -> str:
    """Look something up."""
    return f"result {n}"


def _recording_view(seen: list[ViewRequest]):
    def view(request: ViewRequest) -> View:
        seen.append(request)
        if request.first_call:
            return View(base="dashboards: d1 v1")
        return View(changes=f"since v1: call {len(seen)}")

    return view


async def test_the_base_stays_on_the_human_message_and_changes_end_the_latest_one() -> None:
    seen: list[ViewRequest] = []
    model = ScriptedModel(
        script=[{"tool": "lookup", "input": {"n": 1}}, {"tool": "lookup", "input": {"n": 2}}],
        final_text="done",
    )
    agent = Agent(
        model=model,
        tools=[lookup],
        hooks=[ContextHook(views=[_recording_view(seen)])],
        callback_handler=None,
    )
    set_activity_context("thread-1", "run-1")
    try:
        await agent.invoke_async("build it")
    finally:
        set_activity_context(None)

    first, second, third = model.calls
    assert first[0]["content"][0] == {"text": "dashboards: d1 v1"}
    assert second[0]["content"][0] == {"text": "dashboards: d1 v1"}
    assert third[0]["content"][0] == {"text": "dashboards: d1 v1"}
    assert second[-1]["content"][-1] == {"text": "since v1: call 2"}
    assert "toolResult" in second[-1]["content"][0]
    assert third[-1]["content"][-1] == {"text": "since v1: call 3"}
    assert not any(
        block == {"text": "since v1: call 2"} for message in third for block in message["content"]
    )
    assert [request.first_call for request in seen] == [True, False, False]
    assert (seen[0].thread_id, seen[0].run_id) == ("thread-1", "run-1")
    assert agent.messages[0]["content"] == [{"text": "build it"}]


async def test_a_failing_view_is_left_out() -> None:
    def broken(_request: ViewRequest) -> View:
        raise RuntimeError("service down")

    model = ScriptedModel(final_text="done")
    agent = Agent(model=model, hooks=[ContextHook(views=[broken])], callback_handler=None)
    await agent.invoke_async("hello")
    assert model.calls[0][0]["content"] == [{"text": "hello"}]


def sample_view(request: ViewRequest) -> View | None:
    """Import-spec target for the config test."""
    return View(base=f"view for {request.agent}") if request.first_call else None


async def test_agents_take_views_from_config() -> None:
    model = ScriptedModel(final_text="done")
    agent = build_agent_from_def(
        "analyst",
        agent_def(model="m", views=["tests.runtime.test_working_views:sample_view"]),
        models={"m": model},
        mcp_clients={},
    )
    await agent.invoke_async("hello")
    sent: list[Any] = model.calls[0]
    assert {"text": "view for analyst"} in sent[0]["content"]


async def test_a_view_reads_the_shared_state_as_of_each_call() -> None:
    seen: list[list[str]] = []

    def workspace(request: ViewRequest) -> View:
        seen.append(sorted(request.state.get("dashboards", {})))
        return View(changes=f"dashboards: {sorted(request.state.get('dashboards', {}))}")

    channel = StateChannel(state={"dashboards": {"d-1": {"title": "Sales"}}})

    @tool
    def add_dashboard() -> str:
        """Add a dashboard."""
        channel.state["dashboards"]["d-2"] = {"title": "Churn"}
        return "added"

    model = ScriptedModel(script=[{"tool": "add_dashboard", "input": {}}], final_text="done")
    agent = Agent(
        model=model,
        tools=[add_dashboard],
        hooks=[ContextHook(views=[workspace])],
        callback_handler=None,
    )
    set_state_channel(channel)
    try:
        await agent.invoke_async("extend it")
    finally:
        set_state_channel(None)

    assert seen == [["d-1"], ["d-1", "d-2"]]
    assert model.calls[1][-1]["content"][-1] == {"text": "dashboards: ['d-1', 'd-2']"}
