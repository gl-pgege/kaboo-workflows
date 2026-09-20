"""Pause a run that has filled its response, so it continues in the next one.

``ContinuationHook`` is the agent-side half of :class:`
~kaboo_workflows._context.ResponseBudget`. Where the budget counts what has
been written, this decides where to stop.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import BeforeToolCallEvent

from .._context import get_response_budget

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

logger = logging.getLogger(__name__)

#: Reason payload marking an interrupt as a continuation rather than a question
#: for the user. Clients resolve it immediately and without any prompt; see
#: ``kaboo-react``'s interrupt handler.
CONTINUATION_TYPE = "continuation"


class ContinuationHook(HookProvider):
    """Interrupt before a tool call once the response has used its byte budget.

    A long run can outgrow the host's per-response limit, and a response cut
    off at the limit loses the whole turn. Pausing costs nothing by comparison:
    a tool boundary is already a resumable point — it is where human-in-the-loop
    approval pauses — so the run stops there, the response ends with a proper
    terminal event, and the client resumes into a response with a fresh budget.
    The turn id is unchanged across the resume, so the user sees one turn.

    The pause is an ordinary strands interrupt, which is what makes it safe: the
    pending tool call and the agent's state are persisted and restored by the
    machinery that already serves approvals. It differs only in who answers it —
    the client, at once, rather than a person.

    The hook is inert unless a budget is bound for the request and that budget
    is spent, so it costs one context read per tool call otherwise.
    """

    @override
    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._on_before_tool)

    def _on_before_tool(self, event: BeforeToolCallEvent) -> None:
        budget = get_response_budget()
        if budget is None or not budget.exhausted:
            return

        logger.info(
            "response budget spent at %d of %d bytes; pausing before %s",
            budget.sent,
            budget.limit,
            event.tool_use.get("name", ""),
        )
        # Raises on the way out and returns the client's acknowledgement on the
        # way back in. The next response starts a new budget, so this returns
        # early then and the tool proceeds.
        event.interrupt(
            name="kaboo:continuation",
            reason={
                "type": CONTINUATION_TYPE,
                "message": "Continuing in a new response",
            },
        )
