# Multi-turn history

A delegate sub-agent starts fresh every turn by default. Set `history: true` and
its transcript is seeded from `state.kaboo_history` on the way in and captured
back into the outgoing snapshot on the way out — so it can remember across turns.

```
assistant_team (delegate):
  coordinator ─▶ memo (history: true)
```

## Config

```yaml
agents:
  memo:
    history: true
    system_prompt: "Use the notes from previous turns when answering follow-ups."

orchestrations:
  assistant_team:
    mode: delegate
    entry_name: coordinator
    connections:
      - { agent: memo, description: "Records and recalls notes across turns" }

entry: assistant_team
```

Full runnable project: [`examples/19_multiturn_history`](https://github.com/gl-pgege/kaboo-workflows/tree/main/examples/19_multiturn_history).

## A delegate orchestration remembers as its entry agent

A delegate orchestration runs a fork of its `entry_name` agent under the
orchestration's own name. That fork reads and writes the transcript its entry
agent's `history:` names, so `analyze_sql` below shares `sql_analyst`'s `sql`
group, exactly as `sql_analyst` would if it were connected directly:

```yaml
agents:
  sql_analyst:
    history: {group: sql}

orchestrations:
  analyze_sql:
    mode: delegate
    entry_name: sql_analyst
    connections:
      - { agent: query_runner, description: "Runs a SQL query" }
  team:
    mode: delegate
    entry_name: coordinator
    connections:
      - { agent: analyze_sql, description: "Answers questions with SQL" }
```

A fork whose entry agent has history off gets nothing, and the entry
orchestration never does: its transcript is the chat.

## Keeping a masked memory

A remembering agent replays everything it ever did on every later turn,
including tool results its reports have long since summarised.
`keep: masked` writes back a lighter copy:

```yaml
agents:
  sql_analyst:
    history: {group: sql, keep: masked}
```

Each finished call keeps its brief, its text, its tool calls and its final
report; it loses its reasoning and the `structuredContent` copy strands keeps of
an MCP tool's result, and each tool result becomes
`Result cleared (<tool>[, <id>=<value>…]): call it again to see it.` A result
survives when it is shorter than the agent's `context.clear_tool_results.min_chars`
(1500 without one), when its tool is in that block's `keep_tools`, or when it is
a delegate's report.

- **Only the written-back copy is masked.** Within the run the agent keeps its
  whole transcript, so a second call in the same run still sees everything.
- **A call paused on an interrupt is written back whole.** It has not finished;
  the resume needs every result.
- **A memory stored in full is masked when the agent loads it**, so switching an
  agent to `masked` takes effect on its existing memory.

`keep: full`, the default, writes back exactly what was captured.

## A summary backstop

Masking still grows with every call. `summarize_over` caps it:

```yaml
models:
  cheap: {provider: bedrock, model_id: us.anthropic.claude-haiku-4-5-20251001-v1:0}

agents:
  sql_analyst:
    history:
      group: sql
      keep: masked
      summarize_over: 20000       # estimated tokens, four characters each
      summarize_model: cheap
```

When the masked memory still exceeds `summarize_over`, `summarize_model` condenses
its oldest calls into one leading pair: a user message, `Summary of your earlier
work in this conversation`, and the summary. The newest calls that fit three
quarters of the budget stay as they were, and so does a paused call. The
summariser is told to keep IDs, decisions, exact figures and open issues.

Anything a report marks to keep goes in verbatim. A block of the final report
that opens with a `To remember` heading or line runs to the next heading of the
same level (a plain line runs to the next heading of any level) and is copied
into the summary as written, through every later summary too:

```markdown
## Findings
Revenue for 2024 was $42.1M.

## To remember
- Revenue is stored in cents in `sales.amount`.
- Query `8f3c` is the one the finance team signed off.
```

If summarising fails, the masked memory is written back as it is and a warning
is logged. `summarize_over` requires `keep: masked`, and `summarize_model` must
name a model under `models:`; both are checked when the config loads.

## Loaded memory is cached on Bedrock

When a remembering agent on a Bedrock model is seeded with memory, the copy its
model is sent ends the last loaded message with a `cachePoint`, so every later
model turn of the call reads the memory from Bedrock's prompt cache. If turning
foreign calls into notes leaves the cache point opening a message, it moves to
the end of the message before, since Bedrock rejects a cache point with nothing
before it. A model with its own `cache_config` is left alone, since strands then
places its cache points itself. Cache points are never written back.

The run's usage in the activity stream (`usageByRun`) carries `cacheHitRate`,
the share of the prompt read from the cache, for any run that read or wrote it:
`cacheReadInputTokens / (inputTokens + cacheReadInputTokens + cacheWriteInputTokens)`.
Bedrock counts the three separately, so `inputTokens` alone is only the
uncached part.

## What the model is sent is not what is stored

A transcript can hold calls to tools the agent about to run does not have — a
shared group's other agents, or an earlier version of the workflow. The model is
sent those as short notes, and an agent with `context.clear_tool_results` is sent
its older tool results cleared; see
[Chapter 8](../configuration/Chapter_08.md#what-the-model-is-sent-context). Both
change only what the model is sent. The chat the client stores and the history
written back keep the original calls.

## The client owns storage

kaboo enriches the data structure; it does **not** choose your datastore. The
captured transcript comes back in the run's outgoing history snapshot, and the
client feeds it back in `state.kaboo_history` on the next turn. For turnkey
persistence (in-memory or Postgres `ThreadStore`s behind a CopilotKit runtime),
use [kaboo-runtime](https://github.com/gl-pgege/kaboo-runtime).

## The state channel carries more than history

`kaboo_history` has a sibling. A pending human-in-the-loop gate travels the same
way, under `kaboo_session`, for the same reason: the run is stateless, so anything
the conversation accumulates has to arrive with the turn. That is what lets an
approval survive a restart — see
[human-in-the-loop](human-in-the-loop.md#a-gate-outlives-the-process-that-opened-it).

Together, the two keys are the reason a server can rebuild its agents per run and
still be indistinguishable from one that kept them: a session holds behaviour,
the state channel holds memory.

## Run

```bash
OPENROUTER_API_KEY=... uv run kaboo-serve examples/19_multiturn_history/config.yaml
```

## Proven by

- `tests/e2e/test_cross_cutting.py::test_subagent_history_round_trips_and_accumulates`
  (turn 2 is seeded from turn 1 and grows).
- `tests/e2e/test_cross_cutting.py::test_history_disabled_sub_agent_does_not_persist`
  (the stateless-by-default contrast).
- `tests/e2e/test_cross_cutting.py::test_a_forked_delegate_entry_remembers_through_its_entry_agents_group`
  (a fork remembers through its entry agent's group).
- `tests/e2e/test_cross_cutting.py::test_a_masked_sub_agent_writes_back_a_paused_call_whole_and_a_finished_one_masked`
  and `tests/runtime/test_masked_history.py` (masking on write-back and on load).
- `tests/e2e/test_cross_cutting.py::test_a_memory_over_summarize_over_is_written_back_with_its_oldest_calls_summarised`
  and `tests/runtime/test_memory_summary.py` (the summary keeps ids and `To remember` blocks).
- `tests/e2e/test_model_input.py` and `tests/runtime/test_memory_cache_point.py`
  (notes, the cache point, and what the client keeps).
