"""Tool loading and wrapping utilities.

Provides helpers for:
- Loading ``@tool``-decorated functions from files, modules, and directories.
- Wrapping ``Agent`` / ``MultiAgentBase`` nodes as ``AgentTool`` instances
  (``node_as_tool``, ``node_as_async_tool``) for delegation.
- Serializing multi-agent results with full execution metadata.
"""

from __future__ import annotations

from .ask_user import ask_user
from .extractors import serialize_multiagent_result
from .fetching import (
    ConfiguredReferenceFetcher,
    ReferenceFetcher,
    fetch_reference_bytes,
    get_reference_fetcher,
    set_reference_fetcher,
)
from .loaders import (
    load_tool_function,
    load_tools_from_directory,
    load_tools_from_file,
    load_tools_from_module,
    resolve_tool_spec,
    resolve_tool_specs,
)
from .references import fetch_attachment, list_references
from .wrappers import (
    node_as_async_tool,
    node_as_tool,
)

__all__ = [
    "ConfiguredReferenceFetcher",
    "ReferenceFetcher",
    "ask_user",
    "fetch_attachment",
    "fetch_reference_bytes",
    "get_reference_fetcher",
    "list_references",
    "set_reference_fetcher",
    "load_tool_function",
    "load_tools_from_directory",
    "load_tools_from_file",
    "load_tools_from_module",
    "node_as_async_tool",
    "node_as_tool",
    "resolve_tool_spec",
    "resolve_tool_specs",
    "serialize_multiagent_result",
]
