# Ask the user, and request inputs

An agent that guesses when it should ask produces confident wrong answers. A
question, a file upload or a choice of data source is a tool the browser runs:
the client declares it with each run, the agent calls it, the run waits, and the
person's answer goes back to the agent that asked
([Human-in-the-loop](../workflows/human-in-the-loop.md) has the protocol). The
server needs no code for it, only an allow-list.

## The agent side

```yaml
agents:
  assistant:
    client_tools: [ask_user, request_files, request_datasources]
    system_prompt: |
      When the question is ambiguous, ask once with ask_user, putting every
      open question into that one call with a few options each. When the data
      you need is not attached, call request_datasources rather than asking in
      prose.
```

`client_tools` names the browser's tools this agent may call. Any agent may
have one, delegates included, and an agent never sees a browser tool its list
doesn't name. Questions are usually best from one voice — the agent the person
talks to — but an analyst deep in a delegate can ask too: its question shows in
the main chat under its name, and its coordinator is told what was asked and
answered.

## The browser side

The client declares each tool, with its description and schema, and draws what
the person answers. With kaboo-react:

```tsx
import { useAskUserTool, useRequestTool } from "@pgege/kaboo-react";

function BrowserTools() {
  useAskUserTool();
  useRequestTool({
    name: "request_datasources",
    description: "Ask the person to pick data sources and wait for the choice.",
    parameters: RequestDatasourcesArgs,
    render: DatasourcesPanel,
  });
  return null;
}
```

- **`ask_user`** asks multiple-choice questions: two or more options each,
  `multiple` for multi-select, and an **Other** choice for an answer in the
  person's own words. The answer is a `{question: answer}` map.
- **A request** is any other input. Its panel calls `answer` with what the
  model should read; `skip` tells the model the person declined.
- **Arguments are checked** against the tool's schema. A malformed call is
  answered with an error that names the problem, so the model corrects it
  instead of the person seeing a broken panel.
- **Several requests show in order**: one agent's questions as one card, and
  several agents' requests one after another.

## What the user experiences

- The run pauses; the chat shows the card or panel; the answer resumes the
  agent that asked, mid-thought, rather than starting over.
- There is no time limit. The paused agent travels in the conversation's state,
  so the person can close the browser, come back days later on another device,
  and answer; it also survives a restart or a second replica
  ([A pause outlives the process](../workflows/human-in-the-loop.md#a-pause-outlives-the-process-that-opened-it)).
- If the person sends a message instead of answering, the request is skipped
  and the agent answers the message
  ([The person can move on](../workflows/human-in-the-loop.md#the-person-can-move-on)).

## A browser tool that acts without answering

Some browser tools do something rather than ask: opening a canvas beside the
chat, say. Nothing comes back for the agent to wait for, so list them under
`browser_actions` at the top level:

```yaml
browser_actions: [show_canvas]

agents:
  assistant:
    client_tools: [ask_user, show_canvas]
```

An agent whose `client_tools` names one gets a real tool, with the name,
description and schema the client declared, that answers at once. The run
never pauses on it: the agent goes on with its turn in the same run, and the
client carries the call out when it arrives, typically when the call's status
turns `complete`. A thread reloaded later shows the call already complete, so
the client acts only on that transition, not on every render.

## A request that needs the server

When answering a request takes server logic — checking the person may use a
data source, say — make it a server tool that interrupts. Declare it with
`context=True` and call `tool_context.interrupt` with a reason whose `type`
names your kind. Whatever the client sends back on resume becomes the
interrupt's return value:

```python
import json
from typing import Any

from strands import tool
from strands.types.tools import ToolContext


def _answer(response: Any) -> str:
    if isinstance(response, dict) and response.get("status") == "cancelled":
        return "The user skipped the request."
    return json.dumps(response, ensure_ascii=False)


@tool(name="pick_account", context=True)
def pick_account(tool_context: ToolContext, reason: str) -> dict:
    """Pause and ask the user to pick one of their billing accounts.

    Args:
        reason: Why an account is needed, shown to the user.
    """
    response = tool_context.interrupt(name="pick_account", reason={"type": "account", "message": reason})
    return {"status": "success", "content": [{"text": _answer(response)}]}
```

kaboo-react renders the interrupt with the component registered for its type
(`interruptRenderers={{ account: AccountPicker }}`), and shows a minimal card
with the `message` and Skip wherever nothing is registered.

## Test it

A scripted model can call a browser tool, and `Pipeline.turn(tools=..., messages=...)`
answers it the way a browser does ([Testing](../testing.md#browser-tools-approvals-and-resume)).
Lab scenarios:
[question-several](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/question-several.yaml),
[question-subagent](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/question-subagent.yaml)
and
[request-datasources](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/request-datasources.yaml).
