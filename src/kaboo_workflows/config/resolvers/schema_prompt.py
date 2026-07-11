"""System prompt supplement for cross-agent schema awareness.

Generates a description of delegate agents' output schemas and appends it
to the orchestrator's system prompt so it knows what structured data to
expect from each delegate.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from ...utils import load_object

if TYPE_CHECKING:
    from ..schema import AgentDef, DelegateConnectionDef

logger = logging.getLogger(__name__)


def build_delegate_schema_prompt(
    connections: list[DelegateConnectionDef],
    agent_defs: dict[str, AgentDef],
) -> str | None:
    """Generate a system prompt supplement describing delegate output schemas.

    For each connection whose target agent has an ``output_schema``, loads the
    Pydantic model and serializes its JSON schema into the prompt supplement.

    Args:
        connections: Delegate connection definitions.
        agent_defs: All declared agent definitions keyed by name.

    Returns:
        A prompt supplement string, or ``None`` if no delegates have output schemas.
    """
    sections: list[str] = []

    for conn in connections:
        agent_def = agent_defs.get(conn.agent)
        if agent_def is None or agent_def.output_schema is None:
            continue

        try:
            schema_cls = load_object(agent_def.output_schema, target="output schema")
            schema_json = schema_cls.model_json_schema()
            sections.append(
                f"## {conn.agent}\n"
                f"Description: {conn.description}\n"
                f"Returns structured JSON:\n```json\n{json.dumps(schema_json, indent=2)}\n```"
            )
        except Exception:
            logger.warning(
                "Failed to load output schema for delegate '%s', skipping schema injection",
                conn.agent,
                exc_info=True,
            )

    if not sections:
        return None

    return (
        "\n\n--- Delegate Agent Output Schemas ---\n"
        "The following agents you can delegate to return structured JSON responses. "
        "Use these schemas to understand the data you will receive.\n\n" + "\n\n".join(sections)
    )
