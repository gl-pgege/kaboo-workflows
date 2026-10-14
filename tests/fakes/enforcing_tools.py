"""A tool whose own service enforces approvals, for ``interrupt.tool_approvals`` tests.

``SERVICE`` stands in for that service. ``set_price`` runs only when the call is
covered, by a single-use approval for exactly this call or by a decision
remembered for the call's store, and otherwise replies ``kaboo_approval`` with
a new pending request. Tests play the host: they tell the service about each
decision with :meth:`FakeApprovalService.decide`, as kaboo-runtime's
``onDecision`` would, before resuming the run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from strands import tool


@dataclass
class FakeApprovalService:
    """Records every call, request and decision so tests can assert on them."""

    reply: str = "text"
    invocations: int = 0
    calls: list[dict[str, Any]] = field(default_factory=list)
    requests: dict[str, dict[str, Any]] = field(default_factory=dict)
    single_use: list[dict[str, Any]] = field(default_factory=list)
    remembered: set[str] = field(default_factory=set)
    issued: int = 0

    def reset(self) -> None:
        self.reply = "text"
        self.invocations = 0
        self.calls.clear()
        self.requests.clear()
        self.single_use.clear()
        self.remembered.clear()
        self.issued = 0

    def decide(self, request: str, scope: str = "once", call: dict[str, Any] | None = None) -> None:
        """Approve a pending request: once for its call (or the edited one), or for its store."""
        pending = self.requests.pop(request)
        if scope == "thread":
            self.remembered.add(pending["subject"])
        else:
            self.single_use.append(call or pending["call"])

    def run(self, call: dict[str, Any]) -> dict[str, Any]:
        self.invocations += 1
        subject = f"store:{call['store']}/catalog"
        if call in self.single_use:
            self.single_use.remove(call)
        elif subject not in self.remembered:
            return self._ask(subject, call)
        self.calls.append(call)
        return {
            "status": "success",
            "content": [{"text": f"{call['sku']} now costs {call['price']}"}],
        }

    def _ask(self, subject: str, call: dict[str, Any]) -> dict[str, Any]:
        self.issued += 1
        request = f"req-{self.issued}"
        self.requests[request] = {"subject": subject, "call": call}
        payload = {
            "kaboo_approval": {
                "request": request,
                "subject": {"key": subject, "label": f"catalog changes in {call['store'].title()}"},
                "scopes": ["once", "thread"],
                "message": f"Set the price of {call['sku']} to {call['price']}",
            }
        }
        if self.reply == "structured":
            return {
                "status": "success",
                "content": [{"text": "This change needs approval."}],
                "structuredContent": payload,
            }
        if self.reply == "json":
            return {"status": "success", "content": [{"json": payload}]}
        return {"status": "success", "content": [{"text": json.dumps(payload)}]}


SERVICE = FakeApprovalService()


@tool
def set_price(store: str, sku: str, price: float) -> dict[str, Any]:
    """Set the price of a product variant in a store."""
    return SERVICE.run({"store": store, "sku": sku, "price": price})
