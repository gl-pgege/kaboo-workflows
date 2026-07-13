"""build_agent_from_def wires the reference policy: tools + ReferenceHook.

The ReferenceHook is attached at build time (retained on
``_kaboo_hook_providers``) so it rides the hook-forwarding path to the entry
clone as well as firing on sub-agents. The built-in tools are registered only
when the global ``tool`` flag is set and the agent is in scope.
"""

from __future__ import annotations

from kaboo_workflows.config.resolvers.agents import (
    build_agent_from_def,
    get_agent_hook_providers,
)
from kaboo_workflows.config.schema import AttachmentsDef
from kaboo_workflows.hooks import ReferenceHook
from tests.factories import agent_def
from tests.fakes import FakeModel

_REF_TOOLS = {"list_references", "fetch_attachment"}


def _models():
    return {"fast": FakeModel()}


def _ref_hook(agent) -> ReferenceHook | None:
    for h in get_agent_hook_providers(agent):
        if isinstance(h, ReferenceHook):
            return h
    return None


def test_default_registers_tools_and_hook():
    agent = build_agent_from_def("a", agent_def(model="fast"), _models(), {})
    assert _REF_TOOLS.issubset(set(agent.tool_names))
    hook = _ref_hook(agent)
    assert hook is not None
    assert hook._enabled is True
    assert hook._inline is False


def test_attachments_none_excludes_agent():
    agent = build_agent_from_def("a", agent_def(model="fast", attachments="none"), _models(), {})
    assert not _REF_TOOLS & set(agent.tool_names)
    assert _ref_hook(agent) is None


def test_global_tool_disabled_keeps_hook_without_tools():
    agent = build_agent_from_def(
        "a",
        agent_def(model="fast"),
        _models(),
        {},
        attachments=AttachmentsDef(default="reference", tool=False),
    )
    assert not _REF_TOOLS & set(agent.tool_names)
    assert _ref_hook(agent) is not None


def test_global_default_none_excludes_when_agent_unset():
    agent = build_agent_from_def(
        "a",
        agent_def(model="fast"),
        _models(),
        {},
        attachments=AttachmentsDef(default="none"),
    )
    assert _ref_hook(agent) is None


def test_inline_agent_hook_flagged_inline():
    agent = build_agent_from_def("a", agent_def(model="fast", attachments="inline"), _models(), {})
    hook = _ref_hook(agent)
    assert hook is not None and hook._inline is True
