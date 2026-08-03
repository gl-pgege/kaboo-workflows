"""A recordable side-effect tool for tool-gate (``interrupt.tools``) e2e tests.

``CALLS`` records every execution, letting tests assert the gate held the call
until approval and that an approve-with-edits resume executed the edited
arguments — exactly once.
"""

from typing import Any

from strands import tool

CALLS: list[dict[str, Any]] = []


@tool
def submit_report(title: str) -> str:
    """Submit the final report for filing."""
    CALLS.append({"title": title})
    return f"report '{title}' submitted"
