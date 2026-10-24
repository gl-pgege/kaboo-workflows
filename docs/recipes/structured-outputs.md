# Structured outputs a UI can render

Some results are better as a card than a paragraph: the dashboard an agent
built, an order's status, a list of findings with their sources. Give that
agent an `output_schema` and its result arrives as typed data alongside the
chat, with the schema's name telling the UI how to draw it.

## Declare the schema

```python
from pydantic import BaseModel, Field


class DashboardSnippet(BaseModel):
    """Dashboard produced or updated by this agent, presented as a live card."""

    dashboard_id: str | None = Field(
        default=None,
        description="ID of the dashboard created or updated in this run. Null if none was produced.",
    )
    title: str | None = Field(default=None, description="Dashboard title.")
    summary: str | None = Field(
        default=None, description="One sentence describing what the dashboard shows."
    )


assert DashboardSnippet().dashboard_id is None
```

```yaml
agents:
  visualizer:
    output_schema: ./schemas.py:DashboardSnippet
    stream: {title: "Creating visualizations"}
```

The model reads the class docstring and every field description, so write them
for the model: say what goes in each field and what to do when there is
nothing to report. Optional fields with a `None` default let an agent that
produced nothing say so, instead of inventing an ID to satisfy the schema.

## What the client receives

The agent's `SUBAGENT_FINISHED` event carries the result:

| Field | Value |
|-------|-------|
| `result` | The filled schema, as JSON |
| `metadata.outputSchema` | The class name, here `DashboardSnippet` |

kaboo-react keeps them on the agent's card as `structuredOutput` and
`outputSchemaName`. Register one renderer per schema name in the UI
(`structuredRenderers`); a card with no renderer for its schema shows the
result as a readable list. Keep structured output for the agents
whose result *is* a card — usually a specialist the orchestrator delegates to —
and let the orchestrator answer the user in prose.

## Test it

A scripted model produces the structured output by calling a tool named after
the schema class:

```python
import asyncio
import tempfile
import textwrap
from pathlib import Path

from kaboo_workflows.testing import Pipeline

project = Path(tempfile.mkdtemp())
(project / "schemas.py").write_text(
    textwrap.dedent(
        '''
        from pydantic import BaseModel, Field

        class OrderCard(BaseModel):
            """An order's status, shown as a card."""

            order_id: str | None = Field(default=None, description="The order's number.")
            status: str | None = Field(default=None, description="Where the order is now.")
        '''
    )
)
(project / "config.yaml").write_text(
    textwrap.dedent(
        """
        x-model: &scripted kaboo_workflows.testing:ScriptedModel
        agents:
          assistant:
            model:
              provider: *scripted
              model_id: scripted
              params:
                script: [{tool: tracker, input: "order 42"}]
                final_text: "Order 42 has shipped."
          tracker:
            output_schema: ./schemas.py:OrderCard
            model:
              provider: *scripted
              model_id: scripted
              params:
                script: [{tool: OrderCard, input: {order_id: "42", status: "shipped"}}]
        orchestrations:
          team:
            mode: delegate
            entry_name: assistant
            connections:
              - {agent: tracker, description: "Tracks orders"}
        entry: team
        """
    )
)


async def main() -> None:
    pipe = Pipeline(str(project / "config.yaml"))
    result = await pipe.turn("Where is order 42?")
    pipe.close()

    card = result.group("assistant.tracker")
    assert card["outputSchemaName"] == "OrderCard"
    assert card["result"] == {"order_id": "42", "status": "shipped"}


asyncio.run(main())
```

## Watch for

- **Hooks that end a turn early** must respect the schema: an agent with an
  `output_schema` cannot answer in free text. See the structured variant of
  [`StopOnExpiredSignIn`](guard-hooks.md#stop-when-the-sign-in-expires).
- **Relative paths** in `output_schema` resolve from the config file's
  directory, like tool paths; in a workflow submitted per run, which has no
  file, they resolve from the service's working directory — prefer a module
  path (`my_service.schemas:DashboardSnippet`) there.
