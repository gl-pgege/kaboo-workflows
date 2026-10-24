"""The run's context as each agent's model reads it.

An item whose description starts with ``tool:<name>`` belongs to that browser
tool and reaches only the agents allowed it.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable
from typing import Any

TOOL_SCOPE_PREFIX = "tool:"


def context_for(
    items: Iterable[dict[str, Any]], client_tools: Collection[str]
) -> list[dict[str, Any]]:
    """The context items an agent sees: all but those of browser tools it may not call."""
    kept = []
    for item in items:
        description = str(item.get("description") or "")
        if description.startswith(TOOL_SCOPE_PREFIX):
            words = description[len(TOOL_SCOPE_PREFIX) :].split(maxsplit=1)
            if not words or words[0] not in client_tools:
                continue
        kept.append(item)
    return kept


def format_context(items: Iterable[dict[str, Any]]) -> str:
    """The context as the text block the model reads, in ag-ui-strands' format."""
    lines = []
    for item in items:
        description = str(item.get("description") or "").strip()
        value = item.get("value")
        text = value if isinstance(value, str) else json.dumps(value)
        lines.append(f"- {description}: {text}" if description else f"- {text}")
    return "Context provided by the application:\n" + "\n".join(lines) if lines else ""
