"""HookDef / ConversationManagerDef -> live instance resolution (built-in + custom)."""

from __future__ import annotations

import pytest
from strands.agent.conversation_manager import ConversationManager
from strands.hooks import HookProvider

from kaboo_workflows.config.resolvers.conversation_manager import resolve_conversation_manager
from kaboo_workflows.config.resolvers.hooks import resolve_hook, resolve_hook_entry
from kaboo_workflows.config.schema import ConversationManagerDef, HookDef


def test_builtin_hook_resolves_to_hook_provider():
    hook = resolve_hook(
        HookDef(type="kaboo_workflows.hooks:MaxToolCallsGuard", params={"max_calls": 5})
    )
    assert isinstance(hook, HookProvider)


def test_string_entry_is_treated_as_import_spec():
    hook = resolve_hook_entry("kaboo_workflows.hooks:ToolNameSanitizer")
    assert isinstance(hook, HookProvider)


def test_hook_type_without_colon_raises_value_error():
    with pytest.raises(ValueError, match="import spec"):
        resolve_hook(HookDef(type="not_a_spec"))


def test_hook_resolving_to_non_hook_provider_raises_type_error():
    with pytest.raises(TypeError):
        resolve_hook(HookDef(type="builtins:dict"))


def test_conversation_manager_resolves_from_import_spec():
    cm = resolve_conversation_manager(
        ConversationManagerDef(type="strands.agent:SlidingWindowConversationManager")
    )
    assert isinstance(cm, ConversationManager)


def test_conversation_manager_without_colon_raises_value_error():
    with pytest.raises(ValueError, match="import spec"):
        resolve_conversation_manager(ConversationManagerDef(type="bad"))


def test_conversation_manager_wrong_type_raises_type_error():
    with pytest.raises(TypeError):
        resolve_conversation_manager(ConversationManagerDef(type="builtins:dict"))
