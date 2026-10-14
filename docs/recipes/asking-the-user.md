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
    system_prompt: |
      When the question is ambiguous, ask once with ask_user, putting every
      open question into that one call with a few options each. When the data
      you need is not attached, call request_datasources rather than asking in
      prose.
```

Grant `ask_user` to the agent that talks to the user, and usually not to the
analysts it delegates to: questions should come from one voice, and a
sub-agent's question pauses the whole run anyway.

## Questions are multiple choice

`ask_user` only asks multiple-choice questions: two or more options each,
`multiple` for multi-select, and an **Other** choice the person can always use
to answer in their own words. Prompts do not need to mention Other — the card
adds it, and the tool strips one the model lists. A free-text question is
refused with a tool error that teaches the rule, so the model rephrases it with
options.

## A custom request is a tool that interrupts

An input that is not a question — a file, a data source — is its own request
kind. Any tool can pause the run: declare it with `context=True` and call
`tool_context.interrupt` with a reason whose `type` names your kind. Whatever
the client sends back on resume becomes the interrupt's return value, and the
tool turns it into a result the model can read:

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
        return "The user skipped the request."
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
            "type": "datasources",
            "message": reason,
            "kinds": list(kinds) if kinds else ["sql", "kafka", "prometheus"],
        },
    )
    return {"status": "success", "content": [{"text": _answer(response)}]}
```

The `reason` payload is the contract with your UI:

- **`type`** names the kind. kaboo-react renders it with the component you
  register for it (`interruptRenderers={{ datasources: DatasourcePicker }}`),
  and shows a minimal card with the `message` and Skip wherever nothing is
  registered, so the chat never stalls.
- **`message`** says what the request is for.
- **Your own fields** (`kinds` here) reach the renderer unchanged in the
  interrupt's `metadata`.
- **A skipped request is an answer.** It arrives as `{"status": "cancelled",
  "skipped": True}`; tell the model the user skipped it, so it can continue
  without the input instead of asking again.

## What the user experiences

- The run pauses; the chat shows the card or panel; the answer resumes the
  *same* run, so the agent continues mid-thought rather than starting over.
- There is no time limit. The pause lives in the stored conversation, so the
  person can close the browser, come back days later on another device, and
  answer; it also survives a restart or a second replica
  ([A gate outlives the process](../workflows/human-in-the-loop.md#a-gate-outlives-the-process-that-opened-it)).
- If the person sends a message instead of answering, the request is skipped
  and the agent answers the message
  ([The person can move on](../workflows/human-in-the-loop.md#the-person-can-move-on)).

## Test it

A scripted model can call your request tool, and `Pipeline.turn(resume=...)`
answers it, including on a fresh pipeline to prove the restart case
([Testing](../testing.md#interrupts-and-resume)).
