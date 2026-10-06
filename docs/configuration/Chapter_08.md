# Chapter 8: Conversation Managers — Controlling Context Windows

[← Back to Table of Contents](README.md) | [← Previous: Session Persistence](Chapter_07.md)

---

Conversation managers control how the conversation history is managed — truncation, summarization, or no management at all. This is different from session persistence (which stores history) — conversation managers decide **what's in the context window** when the LLM is called.

```yaml
agents:
  assistant:
    model: default
    conversation_manager:
      type: strands.agent:SlidingWindowConversationManager
      params:
        window_size: 40
        should_truncate_results: false
    system_prompt: "You are a helpful assistant."
```

## Built-in Conversation Managers

These come from strands-agents directly:

| Class | What It Does |
|-------|-------------|
| `strands.agent:SlidingWindowConversationManager` | Keeps the last N messages in context |
| `strands.agent:SummarizingConversationManager` | Summarizes older messages to save context |
| `strands.agent:NullConversationManager` | No management — keeps entire history |

## Using Them

```yaml
# Fixed-window approach
conversation_manager:
  type: strands.agent:SlidingWindowConversationManager
  params:
    window_size: 20

# Summarization approach
conversation_manager:
  type: strands.agent:SummarizingConversationManager
  params:
    summary_ratio: 0.3
    preserve_recent_messages: 10

# No management (pass everything)
conversation_manager:
  type: strands.agent:NullConversationManager
```

## Custom Conversation Managers

Write your own by subclassing `strands.agent.conversation_manager.ConversationManager`:

```yaml
conversation_manager:
  type: ./managers.py:MySmartManager
  params:
    strategy: semantic_relevance
```

## What the Model Is Sent: `context:`

A conversation manager decides what an agent keeps. `context:` shapes only the
copy of that conversation each model call is sent: the agent's own messages, the
chat a client stores and the history written back for it stay exactly as they
were.

### Clearing old tool results

An agent working through many tool calls resends every earlier result on every
model turn. `clear_tool_results` replaces the older ones once the input grows:

```yaml
agents:
  analyst:
    mcp: [warehouse]
    context:
      clear_tool_results:
        trigger: 60000            # estimated input tokens before anything is cleared
        keep_last: 3              # results of the 3 most recent turns always stay
        min_chars: 1500           # shorter results always stay
        keep_tools: [warehouse___get_schema]
```

| Key | Default | Meaning |
|-----|---------|---------|
| `trigger` | *(required)* | Estimated input tokens (system prompt plus messages, four characters a token) above which results are cleared before a model call. The `structuredContent` strands keeps beside an MCP tool's result isn't counted, since no model is sent it. |
| `keep_last` | `3` | How many of the most recent turns keep their results; at least 1. A turn is every result of one model response, so a batch of parallel calls stays or goes together, and the newest batch, which the model has not read yet, is always sent whole. |
| `min_chars` | `1500` | Results shorter than this many characters always stay. |
| `keep_tools` | `[]` | Tools whose results always stay. |

A cleared result keeps its call and reads:

```text
Result cleared (warehouse___run_query, queryExecutionId=8f3c, tableId=sales): call it again to see it.
```

It names the tool and up to ten `key=value` pairs found in the result's JSON for
keys ending in `id`, `Id` or `ID`, so the model can call again for the data it
needs. A cleared result also loses its `structuredContent`. A delegate's
result, another agent's report, is never cleared: it cannot be fetched again
without rerunning that agent. The rewrite depends on nothing but
the messages and leaves a cleared result as it is, so the result reads the same
on every later call and a cached prompt prefix survives.

### Calls to tools an agent does not have

Every agent, with or without `context:`, turns a `toolUse` for a tool it does not
have into a text note on the copy its model is sent:

```text
Earlier step (analyze_sql, a tool you don't have): asked {"input": "revenue in 2024"}; returned Revenue in 2024 was $42M.
```

Such calls reach an agent through a transcript it did not write: the chat, when
the workflow changed between turns, or a `history.group` shared with an agent
whose tools differ. A model shown a call it cannot make tends to make it anyway.
A delegate (a name without `___`) keeps up to 2,000 characters of input and its
whole result; an MCP tool (`<target>___<tool>`) keeps 500 characters of input
and a 500-character excerpt of its result, with its full length noted. Calls to
tools the agent has are left alone.

> **Tips & Tricks**
>
> - Conversation managers are per-agent. Different agents in the same config can use different strategies.
> - For orchestration coordinators that do a lot of delegating, `SlidingWindowConversationManager` with a generous `window_size` prevents context overflow from long delegate tool results.
> - Unlike session managers, there's no "global" conversation manager — set it on each agent that needs one.

---

[Next: Chapter 9 — MCP →](Chapter_09.md)
