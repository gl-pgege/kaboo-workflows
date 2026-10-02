"""Delegation wrapping — node_as_tool / node_as_async_tool naming and the result the parent gets.

Result/message extraction (``extractors.py``) is covered in
``runtime/test_result_extraction.py``; this file stays focused on the wrapping.
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel
from strands import Agent

from kaboo_workflows.tools import node_as_async_tool, node_as_tool
from tests.fakes import FakeModel, ScriptedModel


class Verdict(BaseModel):
    passed: bool
    notes: str


def _agent(agent_id: str) -> Agent:
    return Agent(model=FakeModel(), agent_id=agent_id)


def _coordinator(delegate) -> Agent:
    return Agent(
        model=ScriptedModel(
            script=[{"tool": "reviewer", "input": "review the dashboard"}, {"text": "Reviewed."}]
        ),
        tools=[delegate],
        callback_handler=None,
    )


def _delegate_result(coordinator: Agent) -> list:
    return next(
        block["toolResult"]["content"]
        for message in coordinator.messages
        for block in message["content"]
        if "toolResult" in block
    )


def test_node_as_tool_defaults_name_to_agent_id():
    tool = node_as_tool(_agent("helper"), description="Delegate to helper")
    assert tool.tool_name == "helper"


def test_node_as_tool_accepts_explicit_name():
    tool = node_as_tool(_agent("helper"), name="ask_helper", description="d")
    assert tool.tool_name == "ask_helper"


def test_node_as_async_tool_defaults_name_to_agent_id():
    tool = node_as_async_tool(_agent("worker"), description="d")
    assert tool.tool_name == "worker"


@pytest.mark.parametrize("wrap", [node_as_tool, node_as_async_tool], ids=["sync", "async"])
async def test_a_structured_delegate_hands_its_output_to_the_parent(wrap):
    reviewer = Agent(
        model=ScriptedModel(
            script=[{"tool": "Verdict", "input": {"passed": True, "notes": "all good"}}]
        ),
        agent_id="reviewer",
        structured_output_model=Verdict,
        callback_handler=None,
    )
    coordinator = _coordinator(wrap(reviewer, description="Review the dashboard"))

    await coordinator.invoke_async("check my dashboard")

    [block] = _delegate_result(coordinator)
    assert json.loads(block["text"]) == {"passed": True, "notes": "all good"}


@pytest.mark.parametrize("wrap", [node_as_tool, node_as_async_tool], ids=["sync", "async"])
async def test_a_text_delegate_result_reaches_the_parent_unchanged(wrap):
    reviewer = Agent(
        model=ScriptedModel(script=[{"text": "Looks right."}]),
        agent_id="reviewer",
        callback_handler=None,
    )
    coordinator = _coordinator(wrap(reviewer, description="Review the dashboard"))

    await coordinator.invoke_async("check my dashboard")

    assert _delegate_result(coordinator) == [{"text": "Looks right."}]
