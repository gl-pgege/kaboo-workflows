# Testing your workflows

`kaboo_workflows.testing` lets you test a workflow end to end with no model
provider, no network and no server. Two pieces do the work:

- **`ScriptedModel`** stands in for the LLM. Each agent gets a script of
  steps — call this tool, hand off to that agent, answer with this text — and
  replays it the same way on every run.
- **`Pipeline`** drives a config through the same AG-UI path
  `create_agui_app` serves, in-process, and returns what a client would have
  seen as a `TurnResult`: the AG-UI events, the reply text, the state to carry
  into the next turn, and the activity groups a chat UI renders.

Because the real strands event loop runs, a scripted test exercises your
tools, hooks, prompts, orchestration wiring, interrupts and history exactly
as production does. Only the model's choices are fixed.

Use scripted tests for behaviour and wiring, and [evaluations](evaluation.md)
for answer quality against a real model. The two complement each other.

## A first test

```python
import asyncio

from kaboo_workflows.testing import Pipeline

CONFIG = """
agents:
  assistant:
    system_prompt: You answer questions about orders.
    model:
      provider: kaboo_workflows.testing:ScriptedModel
      model_id: scripted
      params:
        final_text: "Your order shipped yesterday."
entry: assistant
"""


async def main() -> None:
    pipe = Pipeline(CONFIG)
    result = await pipe.turn("Where is my order?")
    pipe.close()

    assert result.finished()
    assert result.text() == "Your order shipped yesterday."


asyncio.run(main())
```

`Pipeline` takes what `load` takes: a file path, a raw YAML string, or a list
of either. In a pytest suite with `pytest-asyncio`, write the body as an
`async def test_…` function instead of calling `asyncio.run`. Call
`pipe.close()` when the config starts MCP servers or clients;
`run_pipeline(config, message)` builds a pipeline, drives one turn and closes
it for you.

## Writing a script

A script is a list of steps, one per model turn. The step index resets when a
new user message arrives and advances on every model call within the turn, so
a turn that calls two tools and then answers is three steps. When the script
runs out, the model replies with `final_text`.

| Step | What the model does |
|------|---------------------|
| `{text: "..."}` | Answers with this text and ends the turn |
| `{tool: lookup, input: "42"}` | Calls a tool. A string input fills the tool's first parameter; a mapping is passed as the full input |
| `{tools: [{tool: a, input: ...}, {tool: b, input: ...}]}` | Calls several tools in one message, as a model making parallel calls does |
| `{handoff: reviewer, message: "..."}` | Hands off to another agent in a swarm |
| `{error: "..."}` | Raises mid-turn, to test how a failure surfaces |

Tool names match the tools strands exposes that turn, exactly first and then
by substring, so a prefixed MCP tool such as `sql___run_query` matches
`{tool: run_query}`. A delegate connection appears as a tool named after the
connected agent, so `{tool: researcher}` delegates to `researcher`.

Give every agent its own scripted model. A model shared by several agents
shares one script and one step counter, and their turns interleave.

The scripted model is as strict as Bedrock about conversation shape: a turn
whose tool results do not pair one-to-one with the previous message's tool
calls raises the same `ValidationException` Bedrock would. A malformed
transcript, such as a replayed answer to an interrupted call, fails your test
instead of failing silently in production.

## Testing your own tools

Test the workflow you ship, not a copy of it. Keep the models in their own
file and the workflow — agents, prompts, tools, orchestration — in another,
referring to models by name. Production loads the real models file; tests load
a scripted one under the same names
([multi-file configs](configuration/Chapter_13.md) merge them):

```text
my_service/
  models.yaml          # production models
  workflow.yaml        # agents, prompts, tools, orchestration
  tools.py
tests/
  models.yaml          # the same model names, scripted
  test_assistant.py
```

```yaml
# my_service/workflow.yaml — relative paths resolve from this file
agents:
  assistant:
    model: assistant-model
    system_prompt: You answer questions about orders.
    tools: [./tools.py:lookup_order]
entry: assistant
```

```yaml
# my_service/models.yaml
models:
  assistant-model:
    provider: bedrock
    model_id: us.anthropic.claude-sonnet-5
```

```yaml
# tests/models.yaml
models:
  assistant-model:
    provider: kaboo_workflows.testing:ScriptedModel
    model_id: scripted
    params:
      script:
        - {tool: lookup_order, input: "42"}
      final_text: "Order 42 has shipped."
```

```{.python notest}
# tests/test_assistant.py
from pathlib import Path

from ag_ui.core import EventType

from kaboo_workflows.testing import Pipeline

ROOT = Path(__file__).parents[1]


async def test_assistant_looks_up_the_order() -> None:
    pipe = Pipeline([str(ROOT / "tests/models.yaml"), str(ROOT / "my_service/workflow.yaml")])
    result = await pipe.turn("Where is order 42?")
    pipe.close()

    tool_results = result.of_type(EventType.TOOL_CALL_RESULT)
    assert "shipped" in tool_results[0].content
    assert result.text() == "Order 42 has shipped."
```

The script decides *that* the tool is called and with what input; the tool
itself runs for real. Assert on its result event, or on whatever side effect it
has, such as a row written to a test database. Name a model per agent in
`workflow.yaml` (`coordinator-model`, `researcher-model`) so each can carry its
own script.

## Asserting what the model was sent

Declare the scripted model under top-level `models:` and the pipeline keeps the
instance at `pipe.infra.models[name]`. Its `calls` hold every conversation it
was sent and `system_prompts` every system prompt, which is how you test that a
prompt was composed the way you meant — for example, that a skill was inlined.

```python
import asyncio

from kaboo_workflows.testing import Pipeline

CONFIG = """
models:
  scripted:
    provider: kaboo_workflows.testing:ScriptedModel
    model_id: scripted
    params: {final_text: "Revenue rose 4%."}
agents:
  analyst:
    model: scripted
    system_prompt: |
      <rules>Cite the table every figure comes from.</rules>
      You analyse revenue.
entry: analyst
"""


async def main() -> None:
    pipe = Pipeline(CONFIG)
    await pipe.turn("How did revenue do?")
    pipe.close()

    model = pipe.infra.models["scripted"]
    assert "<rules>Cite the table" in model.system_prompts[0]
    assert model.calls[0][-1]["content"][0]["text"] == "How did revenue do?"


asyncio.run(main())
```

## Multi-agent runs and activity

`TurnResult.groups` is the activity tree a chat UI renders: one group per
agent stream, nested under the group that called it. The entry agent's own
stream is the chat reply, so it has no group.

```python
import asyncio

from kaboo_workflows.testing import run_pipeline

CONFIG = """
x-model: &scripted kaboo_workflows.testing:ScriptedModel
agents:
  coordinator:
    model:
      provider: *scripted
      model_id: scripted
      params:
        script: [{tool: researcher, input: "find last quarter's revenue"}]
        final_text: "Revenue was 42M."
  researcher:
    stream: {title: "Researching"}
    model: {provider: *scripted, model_id: scripted, params: {final_text: "42M."}}
orchestrations:
  team:
    mode: delegate
    entry_name: coordinator
    connections:
      - {agent: researcher, description: "Finds figures"}
entry: team
"""


async def main() -> None:
    result = await run_pipeline(CONFIG, "What was revenue last quarter?")

    assert result.text() == "Revenue was 42M."
    assert result.agent_names() == {"researcher"}
    assert result.group_ids() == ["coordinator.researcher"]


asyncio.run(main())
```

`children_of(group_id)` walks the tree for nested delegation, and
`group(group_id)` returns one group as the UI holds it.

## Interrupts and resume

A run that pauses on `ask_user` or a gated tool finishes with an `interrupt`
outcome. To resume it, send the next turn the way a client does: the previous
turn's `state()` and `messages()`, plus the answer in `resume`.

```python
import asyncio

from kaboo_workflows.testing import Pipeline

CONFIG = """
agents:
  assistant:
    interrupt: true
    model:
      provider: kaboo_workflows.testing:ScriptedModel
      model_id: scripted
      params:
        script:
          - {tool: ask_user, input: {question: "Delete the draft?", options: ["yes", "no"]}}
        final_text: "Deleted."
entry: assistant
"""


async def main() -> None:
    pipe = Pipeline(CONFIG)
    first = await pipe.turn("Clean up my drafts", thread_id="c1", run_id="r1")
    outcome = first.run_outcome()
    assert outcome.type == "interrupt"

    second = await pipe.turn(
        thread_id="c1",
        run_id="r2",
        state=first.state(),
        messages=first.messages(),
        resume=[
            {
                "interruptId": outcome.interrupts[0].id,
                "status": "resolved",
                "payload": {"answer": "yes"},
            }
        ],
    )
    pipe.close()

    assert second.finished()
    assert second.text() == "Deleted."


asyncio.run(main())
```

To prove a paused run survives a restart, resume on a **new** `Pipeline`
built from the same config. It shares nothing with the first, so the only
thing carrying the pause across is the state you pass — the same guarantee a
deployed service gives. Each new pipeline builds fresh scripted models, so
assert on the resumed tool result rather than on a scripted reply.

## Hooks and guards

Hooks run in a pipeline exactly as in production, so you test one by declaring
it in the config and asserting on what the run did. This scripts a coordinator
into calling its helper twice and checks the built-in tool-call guard refused
the second call:

```python
import asyncio

from ag_ui.core import EventType

from kaboo_workflows.testing import Pipeline

CONFIG = """
x-model: &scripted kaboo_workflows.testing:ScriptedModel
agents:
  coordinator:
    model:
      provider: *scripted
      model_id: scripted
      params:
        script:
          - {tool: helper, input: "first"}
          - {tool: helper, input: "second"}
    hooks:
      - type: kaboo_workflows.hooks:MaxToolCallsGuard
        params: {max_calls: 1}
  helper:
    model: {provider: *scripted, model_id: scripted, params: {final_text: "Done."}}
orchestrations:
  team:
    mode: delegate
    entry_name: coordinator
    connections:
      - {agent: helper, description: "Helps"}
entry: team
"""


async def main() -> None:
    pipe = Pipeline(CONFIG)
    result = await pipe.turn("Get help twice")
    pipe.close()

    results = result.of_type(EventType.TOOL_CALL_RESULT)
    assert any("tool call limit" in r.content for r in results)


asyncio.run(main())
```

Your own hooks work the same way: reference them by import spec
(`type: my_service.hooks:RefuseRepeatedFailures`) and script the model into
the situation the hook exists for. The
[guard hooks recipe](recipes/guard-hooks.md) shows the hooks worth having.

## Runtime-composed workflows

A service that serves a different workflow on every run
([Chapter 17](configuration/Chapter_17.md#when-the-config-itself-is-per-request))
is tested the same way. Pass the base config and `session_config_key`, then
submit each run's workflow in `forwarded_props`:

```python
import asyncio

from kaboo_workflows.testing import Pipeline

BASE = """
models:
  analyst-model:
    provider: kaboo_workflows.testing:ScriptedModel
    model_id: scripted
    params: {final_text: "Revenue rose 4%."}
agents:
  fallback:
    model: analyst-model
    system_prompt: You are a fallback assistant.
entry: fallback
"""

SUBMITTED = """
agents:
  analyst:
    model: analyst-model
    system_prompt: You analyse revenue.
entry: analyst
"""


async def main() -> None:
    pipe = Pipeline(BASE, session_config_key="workflow_config")
    result = await pipe.turn(
        "How did revenue do?", forwarded_props={"workflow_config": SUBMITTED}
    )
    pipe.close()

    assert result.text() == "Revenue rose 4%."
    assert pipe.infra.models["analyst-model"].system_prompts[0] == "You analyse revenue."


asyncio.run(main())
```

Feed it the exact YAML your composer produces, and the test covers the
composition too.

## Evaluations with real models

Scripted tests prove the workflow does what it is told. Whether a real model
tells it the right things is a quality question: run a golden dataset through
`kaboo-workflows eval` or `run_eval` from pytest and gate CI on the scorers
([Evaluation](evaluation.md#offline-evals)). Keep evaluations few and
deliberate — each item is a real model call.
