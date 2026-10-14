"""Interrupt hooks for human-in-the-loop tool gating.

``InterruptHook`` pauses agent execution before matching tool calls, surfacing
an approval request to the person via the AG-UI interrupt protocol, and runs
the call only when the person approves it. Decisions the host remembers arrive
as ``forwardedProps.kaboo_grants`` and let matching calls run without asking.
"""

from __future__ import annotations

import fnmatch
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import BeforeToolCallEvent

from .._context import get_forwarded_props, get_session_exchange

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

if TYPE_CHECKING:
    from collections.abc import Callable

    SubjectFn = Callable[[str, dict[str, Any]], Any]

logger = logging.getLogger(__name__)

APPROVAL_SCOPES = ("once", "thread", "always")
"""Decision scopes, narrowest first: this call, this conversation, from now on."""

GRANTS_PROP = "kaboo_grants"
"""``forwardedProps`` key carrying the decisions the host remembers."""

APPROVALS_STATE_KEY = "kaboo_approvals"
"""``invocation_state`` key recording how each gated call was decided."""

_ASK_USER = "ask_user"
_MAX_REASON_CHARS = 500


@dataclass(frozen=True)
class GateRule:
    """Which tools a gate covers, and how far a decision about them may reach.

    Attributes:
        pattern: Tool name or ``fnmatch`` glob (``merchant___*``).
        remember: Widest scope for these tools, or ``None`` for the hook's.
        subject: Callable ``(tool_name, tool_input) -> {key, label} | str |
            None`` deciding what a remembered decision covers, or ``None`` to
            cover the whole tool. Returning ``None`` means no approval needed.
    """

    pattern: str
    remember: str | None = None
    subject: SubjectFn | None = None


class InterruptHook(HookProvider):
    """Gate tool calls on the person's approval via strands interrupts.

    When the agent calls a tool matching one of ``tools``, this hook raises an
    approval request (``event.interrupt()``) describing the call, the subject a
    remembered decision would cover and the scopes the person may choose. The
    call runs only when the answer is ``approved``; anything else — a denial,
    a skip, an expired request — cancels it with a message the model can read.

    A permission the host remembers (``forwardedProps.kaboo_grants``) for the
    same tool and subject lets the call run without asking. Scopes wider than
    ``once`` are offered only when the host remembers decisions and the run
    publishes its open requests to the host.

    An approval may carry an edited ``tool_input`` (AG-UI ``approveWithEdits``):
    the call then runs with the person's arguments instead of the agent's.
    """

    def __init__(
        self,
        tools: list[str | GateRule],
        *,
        agent_name: str = "",
        ttl_seconds: int | None = None,
        skip_read_only: bool = False,
        remember: str = "once",
    ) -> None:
        """Configure which tool calls wait for approval.

        Args:
            tools: Tool names, globs or :class:`GateRule` objects. The first
                matching entry applies. ``ask_user`` is never gated.
            agent_name: Agent name, used to name the interrupt.
            ttl_seconds: Optional deadline for each request. An answer arriving
                after it does not run the call.
            skip_read_only: Let tools annotated ``readOnlyHint: true`` run
                without asking.
            remember: Default widest scope: ``once``, ``thread`` or ``always``.

        Raises:
            ValueError: If a scope is not one of :data:`APPROVAL_SCOPES`.
        """
        self._rules = [t if isinstance(t, GateRule) else GateRule(pattern=t) for t in tools]
        for scope in [remember, *(r.remember for r in self._rules if r.remember is not None)]:
            if scope not in APPROVAL_SCOPES:
                raise ValueError(
                    f"Unknown approval scope {scope!r}; use one of {list(APPROVAL_SCOPES)}."
                )
        self._agent_name = agent_name
        self._ttl_seconds = ttl_seconds
        self._skip_read_only = skip_read_only
        self._remember = remember

    @override
    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._on_before_tool)

    def _on_before_tool(self, event: BeforeToolCallEvent) -> None:
        tool_name = event.tool_use.get("name", "")
        rule = self._rule_for(tool_name, event.selected_tool)
        if rule is None:
            return
        tool_input = event.tool_use.get("input", {})
        subject = _subject_of(rule, tool_name, tool_input, event.selected_tool)
        if subject is None:
            return

        name = f"{self._agent_name}:gate:{tool_name}"
        response = _earlier_answer(event, name)
        if response is None:
            grant = _matching_grant(tool_name, subject["key"])
            if grant is not None:
                _record(
                    event, {"decision": "approved", "by": "permission", "scope": grant["scope"]}
                )
                return
            response = event.interrupt(
                name=name, reason=self._reason(rule, tool_name, tool_input, subject)
            )
        _apply(event, response)

    def _rule_for(self, tool_name: str, tool: Any) -> GateRule | None:
        if tool_name == _ASK_USER:
            return None
        for rule in self._rules:
            if fnmatch.fnmatchcase(tool_name, rule.pattern):
                if self._skip_read_only and _is_read_only(tool):
                    return None
                return rule
        return None

    def _reason(
        self,
        rule: GateRule,
        tool_name: str,
        tool_input: Any,
        subject: dict[str, str],
    ) -> dict[str, Any]:
        reason: dict[str, Any] = {
            "type": "approval",
            "message": f"Agent wants to call {tool_name}",
            "tool_name": tool_name,
            "tool_input": tool_input,
            "subject": subject,
            "scopes": self._offered_scopes(rule),
        }
        if self._ttl_seconds is not None:
            expires = datetime.now(timezone.utc) + timedelta(seconds=self._ttl_seconds)
            reason["expiresAt"] = expires.isoformat().replace("+00:00", "Z")
        return reason

    def _offered_scopes(self, rule: GateRule) -> list[str]:
        widest = rule.remember or self._remember
        scopes = list(APPROVAL_SCOPES[: APPROVAL_SCOPES.index(widest) + 1])
        if len(scopes) > 1 and not _host_remembers():
            return ["once"]
        return scopes


def _is_read_only(tool: Any) -> bool:
    """Whether an MCP tool declares itself read-only (``readOnlyHint: true``)."""
    annotations = getattr(getattr(tool, "mcp_tool", None), "annotations", None)
    return getattr(annotations, "readOnlyHint", None) is True


def _tool_title(tool: Any) -> str | None:
    mcp_tool = getattr(tool, "mcp_tool", None)
    for title in (
        getattr(mcp_tool, "title", None),
        getattr(getattr(mcp_tool, "annotations", None), "title", None),
    ):
        if isinstance(title, str) and title:
            return title
    return None


def _subject_of(
    rule: GateRule, tool_name: str, tool_input: Any, tool: Any
) -> dict[str, str] | None:
    """What a decision about this call covers: ``{key, label}``, or ``None`` for no gate."""
    default = {"key": tool_name, "label": _tool_title(tool) or tool_name}
    if rule.subject is None:
        return default
    try:
        value = rule.subject(tool_name, tool_input if isinstance(tool_input, dict) else {})
    except Exception:
        logger.warning(
            "tool=<%s> | approval subject failed; asking about the whole tool",
            tool_name,
            exc_info=True,
        )
        return default
    if value is None:
        return None
    if isinstance(value, str) and value:
        return {"key": value, "label": value}
    if isinstance(value, dict) and isinstance(value.get("key"), str) and value["key"]:
        return {"key": value["key"], "label": str(value.get("label") or value["key"])}
    logger.warning(
        "tool=<%s>, subject=<%r> | invalid approval subject; asking about the whole tool",
        tool_name,
        value,
    )
    return default


def _earlier_answer(event: Any, name: str) -> Any:
    """The person's answer to this call's request, when the call is resuming.

    Read before remembered permissions, so the person's own decision on this
    request (and any edits) wins over a permission created by that decision.

    DEPENDS ON strands internal: ``Agent._interrupt_state``.
    """
    state = getattr(getattr(event, "agent", None), "_interrupt_state", None)
    interrupts = getattr(state, "interrupts", None)
    if not isinstance(interrupts, dict):
        return None
    tool_use_id = str(event.tool_use.get("toolUseId", ""))
    for interrupt in interrupts.values():
        if getattr(interrupt, "name", None) == name and f":{tool_use_id}:" in str(interrupt.id):
            return interrupt.response
    return None


def _host_remembers() -> bool:
    """Whether the host can remember a decision wider than this one call.

    It needs both halves: the host sends its remembered permissions (so it
    reads decisions back), and this run publishes its open requests to the host
    (``kaboo_session.approvals``).
    """
    exchange = get_session_exchange()
    if exchange is None or not exchange.publishes_approvals:
        return False
    return isinstance(get_forwarded_props().get(GRANTS_PROP), list)


def _matching_grant(tool_name: str, subject_key: str) -> dict[str, Any] | None:
    grants = get_forwarded_props().get(GRANTS_PROP)
    if not isinstance(grants, list):
        return None
    for grant in grants:
        if (
            isinstance(grant, dict)
            and grant.get("tool") == tool_name
            and grant.get("subject") == subject_key
            and grant.get("scope") in APPROVAL_SCOPES[1:]
        ):
            return grant
    return None


def _apply(event: Any, response: Any) -> None:
    """Run the call only on an ``approved`` answer; cancel it on anything else."""
    if isinstance(response, dict) and response.get("status") == "approved":
        edited = response.get("tool_input")
        if isinstance(edited, dict):
            event.tool_use = {**event.tool_use, "input": edited}
        scope = response.get("scope")
        _record(
            event,
            {
                "decision": "approved",
                "by": "person",
                "scope": scope if scope in APPROVAL_SCOPES else "once",
            },
        )
        return
    decision, message, reason = _refusal(response)
    event.cancel_tool = message
    record: dict[str, Any] = {"decision": decision, "by": "person"}
    if reason:
        record["reason"] = reason
    _record(event, record)


def _refusal(response: Any) -> tuple[str, str, str | None]:
    """Classify a non-approval as ``denied``, ``skipped`` or ``expired``.

    A denial is a resolved ``{"status": "cancelled", "reason"?}``; a skip (the
    Skip button, a message sent instead of answering, or no answer at all) and
    an expired request arrive marked ``skipped`` / ``expired``. Anything else
    that is not an approval counts as a skip.
    """
    if isinstance(response, dict):
        if response.get("expired"):
            return (
                "expired",
                "Expired: the approval request was not answered in time, so this did not run.",
                None,
            )
        if response.get("status") == "cancelled" and not response.get("skipped"):
            reason = response.get("reason")
            if isinstance(reason, str) and reason.strip():
                text = reason.strip()[:_MAX_REASON_CHARS]
                return "denied", f"Declined: {text}", text
            return "denied", "Declined.", None
    return (
        "skipped",
        "Skipped: the person moved on without approving this, so it did not run.",
        None,
    )


def _record(event: Any, record: dict[str, Any]) -> None:
    """Note how this call was decided, for its activity entry (see EventPublisher)."""
    state = getattr(event, "invocation_state", None)
    if not isinstance(state, dict):
        return
    tool_use_id = str(event.tool_use.get("toolUseId", ""))
    state.setdefault(APPROVALS_STATE_KEY, {})[tool_use_id] = record
