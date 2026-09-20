"""Reusable HookProvider implementations for strands agents."""

from __future__ import annotations

from .continuation_hook import CONTINUATION_TYPE, ContinuationHook
from .event_publisher import EventPublisher
from .forwarded_props_hook import ForwardedPropsHook
from .history_hook import HistoryHook
from .interrupt_hook import InterruptHook
from .max_calls_guard import MaxToolCallsGuard
from .mcp_meta_hook import MCPCallMetaHook
from .reference_hook import ReferenceHook
from .session_state_hook import (
    SessionStateHook,
    restore_session_state,
    session_state_snapshot,
)
from .stop_guard import MultiAgentStopGuard, StopGuard, stop_guard_from_event
from .tool_name_sanitizer import ToolNameSanitizer

__all__ = [
    "CONTINUATION_TYPE",
    "ContinuationHook",
    "EventPublisher",
    "ForwardedPropsHook",
    "HistoryHook",
    "InterruptHook",
    "MCPCallMetaHook",
    "MaxToolCallsGuard",
    "MultiAgentStopGuard",
    "ReferenceHook",
    "SessionStateHook",
    "StopGuard",
    "ToolNameSanitizer",
    "restore_session_state",
    "session_state_snapshot",
    "stop_guard_from_event",
]
