"""Reusable HookProvider implementations for strands agents."""

from __future__ import annotations

from .context_hook import ContextHook, View, ViewRequest
from .event_publisher import EventPublisher
from .forwarded_props_hook import ForwardedPropsHook
from .history_hook import HistoryHook, HistoryPolicy
from .interrupt_hook import APPROVAL_SCOPES, GRANTS_PROP, GateRule, InterruptHook
from .max_calls_guard import MaxToolCallsGuard
from .mcp_meta_hook import MCPCallMetaHook
from .reference_hook import ReferenceHook
from .session_state_hook import (
    SessionStateHook,
    restore_session_state,
    session_state_snapshot,
)
from .state_effects import StateEffectCall, StateEffectsHook
from .stop_guard import MultiAgentStopGuard, StopGuard, stop_guard_from_event
from .tool_approval_hook import TOOL_APPROVAL_KEY, ToolApprovalHook
from .tool_attachments_hook import ToolAttachmentsHook
from .tool_name_sanitizer import ToolNameSanitizer

__all__ = [
    "APPROVAL_SCOPES",
    "GRANTS_PROP",
    "ContextHook",
    "EventPublisher",
    "ForwardedPropsHook",
    "GateRule",
    "HistoryHook",
    "HistoryPolicy",
    "InterruptHook",
    "MCPCallMetaHook",
    "MaxToolCallsGuard",
    "MultiAgentStopGuard",
    "ReferenceHook",
    "SessionStateHook",
    "StateEffectCall",
    "StateEffectsHook",
    "StopGuard",
    "TOOL_APPROVAL_KEY",
    "ToolApprovalHook",
    "ToolAttachmentsHook",
    "ToolNameSanitizer",
    "View",
    "ViewRequest",
    "restore_session_state",
    "session_state_snapshot",
    "stop_guard_from_event",
]
