from __future__ import annotations

from kaboo_workflows.testing import run_pipeline
from tests.fakes.client import drawn

CONFIG = """
x-model: &m kaboo_workflows.testing:ScriptedModel
agents:
  coordinator:
    model:
      provider: *m
      model_id: scripted
      params:
        script:
          - tool: analyst
            input: "count the regions"
            before: [{text: "I'll ask the analyst."}, {reasoning: "The analyst can query it."}]
        final_text: "There are four regions."
  analyst:
    tools: ["tests.fakes.data_tools:run_query"]
    model:
      provider: *m
      model_id: scripted
      params:
        script:
          - tool: run_query
            input: {sql: "select count(*) from regions"}
            before: [{text: "Counting them."}, {reasoning: "One query is enough."}]
        final_text: "Four."
orchestrations:
  team:
    mode: delegate
    entry_name: coordinator
    connections: [{agent: analyst, description: "Queries the warehouse"}]
entry: team
"""


async def test_every_agent_is_drawn_in_the_order_it_talked_reasoned_and_called() -> None:
    r = await run_pipeline(CONFIG, "How many regions are there?")

    assert not r.errored()
    [run] = {e.subagent_run_id for e in r.events if getattr(e, "subagent_run_id", None)}
    assert drawn(r.events) == [
        "I'll ask the analyst.",
        "reasoning",
        "call analyst",
        "tool",
        "There are four regions.",
    ]
    assert drawn(r.events, run=run) == [
        "Counting them.",
        "reasoning",
        "call run_query",
        "tool",
        "Four.",
    ]
