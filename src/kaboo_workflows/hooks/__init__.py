"""Reusable HookProvider implementations for strands agents."""

from __future__ import annotations

from .event_publisher import EventPublisher
from .forwarded_props_hook import ForwardedPropsHook
from .history_hook import HistoryHook
from .interrupt_hook import InterruptHook
from .max_calls_guard import MaxToolCallsGuard
from .mcp_meta_hook import MCPCallMetaHook
from .reference_hook import ReferenceHook
from .stop_guard import MultiAgentStopGuard, StopGuard, stop_guard_from_event
from .tool_name_sanitizer import ToolNameSanitizer

__all__ = [
    "EventPublisher",
    "ForwardedPropsHook",
    "HistoryHook",
    "InterruptHook",
    "MCPCallMetaHook",
    "MaxToolCallsGuard",
    "MultiAgentStopGuard",
    "ReferenceHook",
    "StopGuard",
    "ToolNameSanitizer",
    "stop_guard_from_event",
]
