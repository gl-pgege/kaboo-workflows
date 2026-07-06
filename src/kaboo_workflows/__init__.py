"""kaboo-workflows — YAML-driven multi-agent orchestration with AG-UI/CopilotKit support."""

from __future__ import annotations

import os as _os

_os.environ["OTEL_SDK_DISABLED"] = "true"
_os.environ["OTEL_PYTHON_DISABLED_INSTRUMENTATIONS"] = "all"

from .config import (
    AppConfig,
    ConfigInput,
    ResolvedConfig,
    ResolvedInfra,
    load,
    load_config,
    load_session,
    resolve_infra,
)
from .config.resolvers.orchestrations import OrchestrationBuilder
from .exceptions import (
    CircularDependencyError,
    ConfigurationError,
    ImportResolutionError,
    SchemaValidationError,
    UnresolvedReferenceError,
)
from .hooks import EventPublisher, MaxToolCallsGuard, StopGuard, ToolNameSanitizer
from .mcp import MCPLifecycle, create_mcp_client, create_mcp_server
from .renderers import AnsiRenderer
from .tools import (
    node_as_async_tool,
    node_as_tool,
    serialize_multiagent_result,
)
from .types import EventType, StreamEvent
from .utils import cli_errors
from .wire import EventQueue, make_event_queue

__all__ = [
    "AnsiRenderer",
    "AppConfig",
    "CircularDependencyError",
    "ConfigInput",
    "ConfigurationError",
    "EventPublisher",
    "EventQueue",
    "EventType",
    "ImportResolutionError",
    "MCPLifecycle",
    "MaxToolCallsGuard",
    "OrchestrationBuilder",
    "ResolvedConfig",
    "ResolvedInfra",
    "SchemaValidationError",
    "StopGuard",
    "StreamEvent",
    "ToolNameSanitizer",
    "UnresolvedReferenceError",
    "cli_errors",
    "create_mcp_client",
    "create_mcp_server",
    "load",
    "load_config",
    "load_session",
    "make_event_queue",
    "node_as_async_tool",
    "node_as_tool",
    "resolve_infra",
    "serialize_multiagent_result",
]
