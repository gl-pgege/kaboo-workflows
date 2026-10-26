"""Tools that fail the ways a sub-agent's tools do, for tool result status e2e tests.

``query_warehouse`` answers with a strands error result. ``create_panel`` answers
as the MCP gateway does when the service behind it rejects a call: a successful
result whose JSON carries the HTTP failure in ``errorMessage``.
"""

import json
from typing import Any

from strands import tool


@tool
def query_warehouse(sql: str) -> dict[str, Any]:
    """Run a SQL query against the warehouse."""
    return {"status": "error", "content": [{"text": "The warehouse is offline."}]}


@tool
def create_panel(kind: str) -> str:
    """Create a dashboard panel."""
    detail = {
        "statusCode": 422,
        "message": f"Unknown panel type: {kind}",
        "fix": "Use one of: bar, line, table",
    }
    return json.dumps(
        {
            "errorMessage": f"HTTP error 422: Unprocessable Entity - {detail}",
            "errorType": "ValueError",
        }
    )
