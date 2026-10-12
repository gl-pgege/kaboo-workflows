# Ask the user, and request inputs

An agent that guesses when it should ask produces confident wrong answers. The
library pauses a run for a question with `ask_user`, gates risky tools behind
an approval, and resumes the same run with the answer
([Human-in-the-loop](../workflows/human-in-the-loop.md) has the protocol).
This recipe adds the parts a product needs on top: custom requests — "attach a
file", "pick a data source" — that a UI renders as its own panel.

## The agent side

```yaml
agents:
  assistant:
    tools:
      - ./tools/requests.py:request_files
      - ./tools/requests.py:request_datasources
    interrupt:
      tools: []             # tools that need approval before running
      ask_user: true        # grant the built-in ask_user tool
      ttl_seconds: 86400    # stamp an expiry the client can show and enforce
    system_prompt: |
      When the question is ambiguous, ask once with ask_user, putting every
      open question into that one call. When the data you need is not
      attached, call request_datasources rather than asking in prose.
```

Grant `ask_user` to the agent that talks to the user, and usually not to the
analysts it delegates to: questions should come from one voice, and a
sub-agent's question pauses the whole run anyway.

## A custom request is a tool that interrupts

Any tool can pause the run. Declare it with `context=True` and call
`tool_context.interrupt`; whatever the client sends back on resume becomes the
interrupt's return value, and the tool turns it into a result the model can
read:

```python
import json
from typing import Any, Literal

from strands import tool
from strands.types.tools import ToolContext

Kind = Literal["sql", "kafka", "prometheus"]


def _answer(response: Any) -> str:
    if response is None:
        return "The user did not provide a selection."
    if isinstance(response, dict) and response.get("status") == "cancelled":
        return "The user declined the request."
    return json.dumps(response, ensure_ascii=False)


@tool(name="request_datasources", context=True)
def request_datasources(
    tool_context: ToolContext, reason: str, kinds: list[Kind] | None = None
) -> dict:
    """Pause and ask the user to select one or more data sources.

    Use this when the analysis needs a data source the run does not have.

    Args:
        reason: Why the data source is needed, shown to the user.
        kinds: The kinds of data source to offer; all when omitted.
    """
    response = tool_context.interrupt(
        name="request_datasources",
        reason={
            "type": "form",
            "kind": "datasources",
            "kinds": list(kinds) if kinds else ["sql", "kafka", "prometheus"],
            "questions": [{"question": reason, "type": "text"}],
        },
    )
    return {"status": "success", "content": [{"text": _answer(response)}]}
```

The `reason` payload is the contract with your UI:

- **`type`** is one the client already renders. kaboo-react renders
  `approval` and `form`; ours registers one `form` renderer that branches on
  **`kind`** to show the file picker or the data source picker.
- **`questions`** keeps the plain form meaningful wherever the custom panel is
  not registered, so an older client degrades to a text question rather than a
  dead end.
- **A cancelled request is an answer.** Tell the model the user declined, so it
  can continue without the input instead of asking again.

## What the user experiences

- The run pauses; the chat shows the panel; the answer resumes the *same* run,
  so the agent continues mid-thought rather than starting over.
- A paused run survives a restart or a second replica: the pending question
  travels in the AG-UI state the client sends back
  ([A gate outlives the process](../workflows/human-in-the-loop.md#a-gate-outlives-the-process-that-opened-it)).
- If the user ignores the question and types something new, the new message
  starts a fresh run. A pending question is restored only when the client
  resumes it.
- `ttl_seconds` only stamps an expiry; enforcing it is the client's or host's
  job.

## Test it

A scripted model can call your request tool, and `Pipeline.turn(resume=...)`
answers it, including on a fresh pipeline to prove the restart case
([Testing](../testing.md#interrupts-and-resume)).
