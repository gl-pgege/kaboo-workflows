"""A tool that saves a query, and the state effect that notes it in the workspace."""

from typing import Any

from strands import tool

from kaboo_workflows.hooks import StateEffectCall


@tool
def save_query(title: str) -> dict[str, Any]:
    """Save a query for later."""
    return {
        "status": "success",
        "content": [
            {"json": {"id": "q_7", "title": title}},
            {"text": f'{{"id": "q_7", "title": "{title}"}}'},
        ],
    }


def saved(call: StateEffectCall) -> list[dict[str, Any]]:
    """Note a saved query under ``/kaboo_workspace/queries``."""
    operations: list[dict[str, Any]] = []
    if "kaboo_workspace" not in call.state:
        operations.append({"op": "add", "path": "/kaboo_workspace", "value": {"queries": {}}})
    operations.append(
        {
            "op": "add",
            "path": f"/kaboo_workspace/queries/{call.result['id']}",
            "value": {"title": call.result["title"], "by": call.agent},
        }
    )
    return operations
