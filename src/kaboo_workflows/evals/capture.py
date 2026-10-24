"""Headless run capture: drive one item through the production wire path.

:class:`EvalPipeline` drives each item through the e2e harness's
:class:`~kaboo_workflows.testing.Pipeline`, which mirrors the AG-UI endpoint —
so an eval run exercises exactly what production serves: the same agents,
orchestration, MCP tools, hooks, and event stream.

It is a client too. A run that waits on a browser tool gets the item's answer
and runs on; an approval request is approved or declined as the item says.
Whatever the item does not answer ends it, and the capture names it.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from ag_ui.core import EventType as AGUIEventType
from ag_ui.core import RunFinishedInterruptOutcome, Tool, ToolMessage

from ..adapters._subagents import fold_subagents
from ..testing.pipeline import Pipeline
from .dataset import EvalItem

_DEFAULT_TIMEOUT_S = 600.0
_MAX_RUNS = 8

ASK_USER = Tool(
    name="ask_user",
    description=(
        "Ask the person one or more short questions and wait for their answers. "
        "Use it only when you cannot go on without their input. Offer options when "
        "there are obvious choices; the person can always type their own answer. "
        "The answers come back as JSON keyed by question."
    ),
    parameters={
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "options": {"type": "array", "items": {"type": "string"}},
                        "multiple": {"type": "boolean"},
                    },
                    "required": ["question"],
                },
            }
        },
        "required": ["questions"],
    },
)
"""kaboo-react's question tool, as a browser declares it."""


def browser_tool(spec: Any) -> Tool:
    """A dataset's browser tool: ``"ask_user"``, or ``{name, description, parameters}``."""
    if spec == ASK_USER.name:
        return ASK_USER
    if isinstance(spec, dict):
        return Tool.model_validate(spec)
    raise ValueError(
        f"browser tool {spec!r}: name ask_user or give name, description and parameters"
    )


@dataclass
class ToolInvocation:
    """One trajectory step: a tool call, or a sub-agent invocation.

    Each sub-agent invocation contributes a step with ``kind="agent"`` (named
    after the invoked agent) followed by that agent's own tool calls
    (``kind="tool"``).
    """

    agent: str
    name: str
    status: str
    input: Any = None
    kind: str = "tool"


@dataclass
class RunCapture:
    """Everything a scorer can look at from one headless run."""

    item_id: str
    text: str = ""
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    tools: list[ToolInvocation] = field(default_factory=list)
    structured_outputs: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    latency_s: float = 0.0
    error: str | None = None
    runs: int = 0
    open_requests: list[str] = field(default_factory=list)
    """Browser tools and approvals the item left unanswered when it ended."""

    def tool_names(self) -> list[str]:
        """Tool names in call order (successful or not)."""
        return [t.name for t in self.tools]


def _capture(
    item_id: str,
    *,
    events: list[Any],
    latency_s: float,
    error: str | None,
    open_requests: list[str],
) -> RunCapture:
    groups = fold_subagents(events)
    text = "".join(
        getattr(e, "delta", "") or ""
        for e in events
        if e.type == AGUIEventType.TEXT_MESSAGE_CONTENT and not getattr(e, "subagent_run_id", None)
    )
    # A delegation shows as the entry's call to the connection and as a group;
    # the group carries more, so the entry call it started is dropped.
    delegated = {g.get("toolCallId") for g in groups.values()}
    tools: list[ToolInvocation] = [
        ToolInvocation(agent="", name=str(e.tool_call_name or ""), status="completed")
        for e in events
        if e.type == AGUIEventType.TOOL_CALL_START
        and not getattr(e, "subagent_run_id", None)
        and e.tool_call_id not in delegated
    ]
    structured: list[dict[str, Any]] = []
    for group in groups.values():
        agent = str(group.get("agentName", ""))
        tools.append(
            ToolInvocation(
                agent=str(group.get("parentGroup") or ""),
                name=agent,
                status=str(group.get("status", "")),
                input=group.get("task"),
                kind="agent",
            )
        )
        for tool in group.get("tools", []):
            tools.append(
                ToolInvocation(
                    agent=agent,
                    name=str(tool.get("toolName", "")),
                    status=str(tool.get("status", "")),
                    input=tool.get("args"),
                )
            )
        if isinstance(group.get("result"), dict):
            structured.append(group["result"])
    usage: dict[str, int] = {}
    finished = [e for e in events if e.type == AGUIEventType.RUN_FINISHED]
    for event in finished:
        for entry in getattr(event, "usage", None) or []:
            for key, value in entry.model_dump(by_alias=True, exclude_none=True).items():
                if isinstance(value, int):
                    usage[key] = usage.get(key, 0) + value
    for e in events:
        if e.type == AGUIEventType.RUN_ERROR and error is None:
            error = getattr(e, "message", None) or "RUN_ERROR"
    return RunCapture(
        item_id=item_id,
        text=text,
        groups=groups,
        tools=tools,
        structured_outputs=structured,
        usage=usage,
        latency_s=latency_s,
        error=error,
        runs=len([e for e in events if e.type == AGUIEventType.RUN_STARTED]),
        open_requests=open_requests,
    )


def _answer_book(answers: dict[str, Any]) -> Callable[[str], str | None]:
    used: dict[str, int] = {}

    def answer(name: str) -> str | None:
        given = answers.get(name)
        if isinstance(given, list):
            index = used.get(name, 0)
            used[name] = index + 1
            given = given[index] if index < len(given) else None
        if given is None:
            return None
        return given if isinstance(given, str) else json.dumps(given)

    return answer


def _call_names(messages: Iterable[Any]) -> dict[str, str]:
    return {
        call.id: call.function.name
        for message in messages
        for call in getattr(message, "tool_calls", None) or []
    }


class EvalPipeline:
    """A resolved workflow that runs dataset items headlessly, one at a time.

    Holds one resolved config and one started MCP lifecycle for the whole
    eval run (items stream through it sequentially — no whole-run buffering).
    Call :meth:`close` (or use ``async with``) when done.
    """

    def __init__(self, config_path: str, *, browser_tools: Iterable[Any] = ()) -> None:
        """Resolve *config_path* and start its MCP servers/clients.

        Args:
            config_path: The workflow config.
            browser_tools: The browser tools every run offers (see :func:`browser_tool`).
        """
        from ..config import parse_config_sources, validate_raw_config
        from ..telemetry import init_telemetry

        init_telemetry(validate_raw_config(parse_config_sources(str(config_path))).telemetry)
        self._pipeline = Pipeline(str(config_path))
        self.app_config = self._pipeline.app_config
        self._browser_tools = [browser_tool(spec) for spec in browser_tools]

    async def __aenter__(self) -> EvalPipeline:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Stop the MCP lifecycle."""
        self._pipeline.close()

    async def run_item(self, item: EvalItem) -> RunCapture:
        """Run one dataset item and capture its outcome.

        Each item gets a fresh thread (no cross-item history) and a wall-clock
        timeout (``item.timeout_s`` or 600s) over all of its runs. Run errors are
        captured on the :class:`RunCapture` rather than raised, so one broken
        item never aborts the dataset.
        """
        thread_id = f"eval-{item.id}-{uuid.uuid4().hex[:8]}"
        events: list[Any] = []
        open_requests: list[str] = []
        error: str | None = None
        started = time.monotonic()
        timeout = item.timeout_s or _DEFAULT_TIMEOUT_S
        try:
            open_requests = await asyncio.wait_for(self._converse(item, thread_id, events), timeout)
        except TimeoutError:
            error = f"timed out after {timeout:.0f}s"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            self._pipeline.forget(thread_id)
        return _capture(
            item.id,
            events=events,
            latency_s=time.monotonic() - started,
            error=error,
            open_requests=open_requests,
        )

    async def _converse(self, item: EvalItem, thread_id: str, events: list[Any]) -> list[str]:
        """Run the item, answering what it can, and return what it left open."""
        answer = _answer_book(item.answers)
        state = item.state or {}
        messages: list[Any] | None = None
        resume: list[dict[str, Any]] | None = None
        for attempt in range(1, _MAX_RUNS + 1):
            result = await self._pipeline.turn(
                item.input,
                thread_id=thread_id,
                run_id=f"run-{item.id}-{attempt}",
                state=state,
                messages=messages,
                resume=resume,
                forwarded_props=item.forwarded_props,
                tools=self._browser_tools,
            )
            events.extend(result.events)
            state = result.state() or state
            transcript = result.messages()
            outcome = result.run_outcome()
            if isinstance(outcome, RunFinishedInterruptOutcome):
                if item.approve is None:
                    return [interrupt.reason or interrupt.id for interrupt in outcome.interrupts]
                resume = [
                    {
                        "interruptId": interrupt.id,
                        "status": "resolved",
                        "payload": {"status": "approved"},
                    }
                    if item.approve
                    else {"interruptId": interrupt.id, "status": "cancelled"}
                    for interrupt in outcome.interrupts
                ]
                messages = transcript
                continue
            pending = getattr(outcome, "pending_tool_call_ids", None) or []
            if not pending:
                return []
            names = _call_names(transcript)
            replies = [
                (call, names.get(call, call), answer(names.get(call, ""))) for call in pending
            ]
            unanswered = [name for _, name, content in replies if content is None]
            if unanswered:
                return unanswered
            messages = [
                *transcript,
                *(
                    ToolMessage(
                        id=f"answer-{call}", role="tool", tool_call_id=call, content=content
                    )
                    for call, _, content in replies
                    if content is not None
                ),
            ]
            resume = None
        return []
