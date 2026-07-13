"""ReferenceHook — manifest injection + inline media on real strands agents.

Mirrors ``test_history_hook``: drive the hook with a real ``Agent`` and real
``BeforeInvocationEvent`` / ``BeforeModelCallEvent`` and assert the observable
contract (system-prompt manifest, inline ``ContentBlock`` prepend, no-ops).
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from typing import Any

import pytest
from strands import Agent
from strands.hooks import HookRegistry
from strands.hooks.events import BeforeInvocationEvent, BeforeModelCallEvent

from kaboo_workflows._context import (
    Reference,
    request_inline,
    set_inline_requests,
    set_references,
)
from kaboo_workflows.hooks import ReferenceHook
from tests.fakes import FakeModel


@pytest.fixture(autouse=True)
def _isolate_refs() -> Iterator[None]:
    set_references(None)
    set_inline_requests(set())
    yield
    set_references(None)
    set_inline_requests(None)


@pytest.fixture
def agent() -> Agent:
    return Agent(model=FakeModel(), system_prompt="Base prompt.")


def _fire_before(hook: ReferenceHook, agent: Agent) -> None:
    registry = HookRegistry()
    registry.add_hook(hook)
    registry.invoke_callbacks(BeforeInvocationEvent(agent=agent))


def _fire_model(hook: ReferenceHook, agent: Agent) -> None:
    registry = HookRegistry()
    registry.add_hook(hook)
    registry.invoke_callbacks(BeforeModelCallEvent(agent=agent))


def _file_ref(**over: Any) -> Reference:
    data = base64.b64encode(b"hello world").decode()
    defaults: dict[str, Any] = dict(
        kind="file",
        id="file_1",
        name="notes.txt",
        transport="attachment",
        mime_type="text/plain",
        source="data",
        value=data,
    )
    return Reference(**{**defaults, **over})


def test_manifest_injected_into_system_prompt(agent: Agent) -> None:
    set_references([_file_ref()])
    _fire_before(ReferenceHook(enabled=True, inline=False, tool_enabled=True), agent)

    prompt = agent.system_prompt
    assert prompt is not None
    assert "Base prompt." in prompt
    assert "Referenced items" in prompt
    assert "notes.txt" in prompt
    assert "file_1" in prompt
    assert "fetch_attachment" in prompt


def test_manifest_omits_tool_hint_when_tool_disabled(agent: Agent) -> None:
    set_references([_file_ref()])
    _fire_before(ReferenceHook(enabled=True, inline=False, tool_enabled=False), agent)
    prompt = agent.system_prompt
    assert prompt is not None
    assert "fetch_attachment" not in prompt


def test_disabled_agent_is_a_noop(agent: Agent) -> None:
    set_references([_file_ref()])
    _fire_before(ReferenceHook(enabled=False, inline=False, tool_enabled=True), agent)
    assert agent.system_prompt == "Base prompt."


def test_no_references_is_a_noop(agent: Agent) -> None:
    _fire_before(ReferenceHook(enabled=True, inline=False, tool_enabled=True), agent)
    assert agent.system_prompt == "Base prompt."


def test_manifest_not_reappended_across_invocations(agent: Agent) -> None:
    set_references([_file_ref()])
    hook = ReferenceHook(enabled=True, inline=False, tool_enabled=True)
    _fire_before(hook, agent)
    _fire_before(hook, agent)
    prompt = agent.system_prompt
    assert prompt is not None
    assert prompt.count("Referenced items") == 1


def test_active_interrupt_leaves_prompt_intact(agent: Agent) -> None:
    set_references([_file_ref()])
    agent._interrupt_state.activated = True
    _fire_before(ReferenceHook(enabled=True, inline=False, tool_enabled=True), agent)
    assert agent.system_prompt == "Base prompt."


def test_inline_prepends_media_block_at_before_model(agent: Agent) -> None:
    png = base64.b64encode(b"\x89PNG\r\n").decode()
    set_references([_file_ref(id="img_1", name="a.png", mime_type="image/png", value=png)])
    agent.messages = [{"role": "user", "content": [{"text": "describe this"}]}]

    hook = ReferenceHook(enabled=True, inline=True, tool_enabled=True)
    _fire_before(hook, agent)  # arms + injects manifest
    _fire_model(hook, agent)  # injects media

    content = agent.messages[-1]["content"]
    assert content[0]["image"]["format"] == "png"
    assert content[-1] == {"text": "describe this"}


def test_inline_injects_media_only_once(agent: Agent) -> None:
    png = base64.b64encode(b"\x89PNG\r\n").decode()
    set_references([_file_ref(id="img_1", name="a.png", mime_type="image/png", value=png)])
    agent.messages = [{"role": "user", "content": [{"text": "hi"}]}]

    hook = ReferenceHook(enabled=True, inline=True, tool_enabled=True)
    _fire_before(hook, agent)
    _fire_model(hook, agent)
    _fire_model(hook, agent)

    images = [b for b in agent.messages[-1]["content"] if "image" in b]
    assert len(images) == 1


def test_reference_mode_does_not_inject_media(agent: Agent) -> None:
    png = base64.b64encode(b"\x89PNG\r\n").decode()
    set_references([_file_ref(id="img_1", name="a.png", mime_type="image/png", value=png)])
    agent.messages = [{"role": "user", "content": [{"text": "hi"}]}]

    hook = ReferenceHook(enabled=True, inline=False, tool_enabled=True)
    _fire_before(hook, agent)
    _fire_model(hook, agent)

    assert all("image" not in b for b in agent.messages[-1]["content"])


def test_reference_mode_injects_fetch_requested_media(agent: Agent) -> None:
    # fetch_attachment records the id; the hook materializes it into the user
    # message (media can't ride a tool-role message on OpenAI-compat providers).
    png = base64.b64encode(b"\x89PNG\r\n").decode()
    set_references([_file_ref(id="img_1", name="a.png", mime_type="image/png", value=png)])
    agent.messages = [{"role": "user", "content": [{"text": "hi"}]}]

    hook = ReferenceHook(enabled=True, inline=False, tool_enabled=True)
    _fire_before(hook, agent)
    request_inline("img_1")
    _fire_model(hook, agent)

    content = agent.messages[-1]["content"]
    assert content[0]["image"]["format"] == "png"


def test_fetch_requested_media_injected_only_once(agent: Agent) -> None:
    png = base64.b64encode(b"\x89PNG\r\n").decode()
    set_references([_file_ref(id="img_1", name="a.png", mime_type="image/png", value=png)])
    agent.messages = [{"role": "user", "content": [{"text": "hi"}]}]

    hook = ReferenceHook(enabled=True, inline=False, tool_enabled=True)
    _fire_before(hook, agent)
    request_inline("img_1")
    _fire_model(hook, agent)
    _fire_model(hook, agent)

    images = [b for b in agent.messages[-1]["content"] if "image" in b]
    assert len(images) == 1
