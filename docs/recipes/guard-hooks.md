# Guard hooks for every agent

Some failures have nothing to do with any particular workflow: a transcript
the provider rejects, a model retrying the same broken call, a user whose
sign-in expires mid-run. Hooks
([Chapter 6](../configuration/Chapter_06.md)) are where to catch them, once,
for every agent. These are the guards Kaboo puts on every agent of every
workflow.

## Answer every tool call before the model sees the transcript

Bedrock rejects a turn unless every tool call in an assistant message is
answered by a tool result in the very next message — and strands reacts to the
rejection by stopping the run silently. Transcripts break this rule more often
than you would think: a turn cut off mid-tools, an interrupted call replayed
from the client, a message the host persisted twice. Bedrock is just as strict
about two messages in a row from the same role, and about empty text blocks.

Repairing the transcript before every model call turns each of those into a
run that carries on:

```python
from typing import Any

from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import BeforeModelCallEvent

UNANSWERED = (
    "This call did not run: the turn that made it ended before it could. "
    "Call it again if you still need its result."
)


def _blank(block: Any) -> bool:
    return isinstance(block, dict) and set(block) == {"text"} and not str(block["text"]).strip()


def alternating(messages: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for message in messages:
        content = [b for b in message.get("content") or [] if not _blank(b)]
        if not content:
            continue
        if merged and merged[-1]["role"] == message["role"]:
            merged[-1] = {**merged[-1], "content": merged[-1]["content"] + content}
        else:
            merged.append({**message, "content": content})
    for message in merged:
        if message["role"] == "user":
            results = [b for b in message["content"] if "toolResult" in b]
            message["content"] = results + [b for b in message["content"] if b not in results]
    return merged


def answered(messages: list[dict]) -> list[dict]:
    messages = alternating(messages)
    repaired: list[dict] = []
    for index, message in enumerate(messages):
        repaired.append(message)
        if message["role"] != "assistant":
            continue
        asked = [b["toolUse"]["toolUseId"] for b in message["content"] if "toolUse" in b]
        following = messages[index + 1] if index + 1 < len(messages) else None
        replies = following if following and following["role"] == "user" else None
        given = {
            b["toolResult"]["toolUseId"]
            for b in (replies or {}).get("content", [])
            if "toolResult" in b
        }
        missing = [
            {"toolResult": {"toolUseId": t, "status": "error", "content": [{"text": UNANSWERED}]}}
            for t in asked
            if t not in given
        ]
        if not missing:
            continue
        if replies is not None:
            replies["content"] = missing + replies["content"]
        else:
            repaired.append({"role": "user", "content": missing})
    return repaired


class AnswerEveryToolUse(HookProvider):
    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeModelCallEvent, self._answer)

    def _answer(self, event: BeforeModelCallEvent) -> None:
        event.agent.messages[:] = answered(event.agent.messages)


broken = [
    {"role": "user", "content": [{"text": "Run the report"}]},
    {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t1", "name": "run", "input": {}}}]},
    {"role": "user", "content": [{"text": "Any news?"}]},
]
fixed = answered(broken)
assert fixed[2]["content"][0]["toolResult"]["toolUseId"] == "t1"
```

The inserted result is an error the model can read, so it calls the tool again
if it still needs it.

## Refuse a call that has already failed twice

A model that gets an error back sometimes sends the identical call again, and
again. Refusing the third identical attempt, with a message telling it to
change course, breaks the loop without ending the run:

```python
import json
from typing import Any

from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import AfterToolCallEvent, BeforeInvocationEvent, BeforeToolCallEvent

REPEATED_FAILURE = (
    "This exact call has already failed twice, so it was not sent again. Read the "
    "earlier errors, then change the arguments or take a different approach."
)


def failed(result: Any) -> bool:
    return isinstance(result, dict) and result.get("status") == "error"


class RefuseRepeatedFailures(HookProvider):
    def __init__(self, limit: int = 2) -> None:
        self.limit = limit
        self.failures: dict[str, int] = {}

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, lambda event: self.failures.clear())
        registry.add_callback(BeforeToolCallEvent, self._refuse)
        registry.add_callback(AfterToolCallEvent, self._count)

    @staticmethod
    def _key(tool_use: Any) -> str:
        return json.dumps([tool_use.get("name"), tool_use.get("input")], sort_keys=True, default=str)

    def _refuse(self, event: BeforeToolCallEvent) -> None:
        if self.failures.get(self._key(event.tool_use), 0) >= self.limit:
            event.cancel_tool = REPEATED_FAILURE

    def _count(self, event: AfterToolCallEvent) -> None:
        key = self._key(event.tool_use)
        if failed(event.result):
            self.failures[key] = self.failures.get(key, 0) + 1
        else:
            self.failures.pop(key, None)
```

The key is the tool name *and* its input: the same tool with different
arguments is a new attempt, which is exactly what the message asks for. Ours
also counts a result whose JSON body says `"success": false` or carries an
`"error"`, since many MCP services report failure that way. The library's
`MaxToolCallsGuard` is the coarser companion: a ceiling on tool calls per
invocation, whatever they are.

## Stop when the sign-in expires

A run relaying the user's token ([Call MCP tools as the signed-in
user](user-identity-mcp.md)) can outlive it, and then every tool call fails
authorization. Check the token's expiry before each model call and each tool
call, and answer with one clear message instead:

```python
import time
from typing import Any

from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import BeforeModelCallEvent, BeforeToolCallEvent
from strands.models import Model

from kaboo_workflows import get_auth_context

SIGNED_OUT = (
    "Your sign-in expired while I was working, so I stopped here. Sign in again "
    "and send your question once more; what I built so far is kept."
)


def signed_out() -> bool:
    principal = get_auth_context()
    expiry = principal.claims.get("exp") if principal else None
    return isinstance(expiry, (int, float)) and time.time() >= expiry


class SignedOutModel(Model):
    def update_config(self, **model_config: Any) -> None:
        pass

    def get_config(self) -> Any:
        return {}

    def structured_output(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def stream(self, *args: Any, **kwargs: Any) -> Any:
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockDelta": {"delta": {"text": SIGNED_OUT}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}


class StopOnExpiredSignIn(HookProvider):
    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeModelCallEvent, self._answer_signed_out)
        registry.add_callback(BeforeToolCallEvent, self._refuse_tool)

    def _answer_signed_out(self, event: BeforeModelCallEvent) -> None:
        if signed_out():
            event.agent.model = SignedOutModel()

    def _refuse_tool(self, event: BeforeToolCallEvent) -> None:
        if signed_out():
            event.cancel_tool = SIGNED_OUT


assert not signed_out()
```

Swapping in a model that replies with the message makes it the agent's answer,
so it reaches the chat like any other reply. An agent with an `output_schema`
cannot answer in prose; on strands versions with `AfterToolsEvent`, ours
cancels such an agent's invocation and ends its turn after the current tools
instead.

## Two small ones

- **Today's date.** Models do not know what day it is. A
  `BeforeInvocationEvent` hook that appends "Today's date is …" in the user's
  time zone (`get_forwarded_props()["timezone"]`) to the system prompt fixes
  every "last month" question.
- **Model timing.** A `BeforeModelCallEvent` / `AfterModelCallEvent` pair that
  logs each call's latency, time to first event and retry count with the run id
  (`get_run_id()`) answers "why was that slow?" without a tracing backend.

## Put them on every agent

Either add them when composing the workflow, as Kaboo does
([Compose a workflow per run](per-run-workflows.md)), or declare them once in
the config and alias them:

```yaml
x-guards: &guards
  - my_service.hooks:AnswerEveryToolUse
  - my_service.hooks:RefuseRepeatedFailures
  - my_service.hooks:StopOnExpiredSignIn

agents:
  analyst:
    hooks: *guards
  visualizer:
    hooks: *guards
```

`x-` blocks exist only to hold anchors; the loader strips them
([Chapter 4](../configuration/Chapter_04.md)).

## Test them

Each guard has a failure it exists for, so script the model into that failure
and assert the run survived it — a broken transcript passed in as `messages`,
the same failing call scripted three times
([Testing](../testing.md#hooks-and-guards)).
