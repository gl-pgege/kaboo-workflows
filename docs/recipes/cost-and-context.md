# Cost and context control

A data-analysis run can make dozens of model calls, each resending a long
system prompt, a tool list and every earlier tool result. Most of the cost —
and most context-window trouble — comes from that resending. Each setting
below is documented in its chapter; this recipe is the combination Kaboo runs,
in order of payoff.

## 1. Cache what every call repeats

```yaml
models:
  default:
    provider: bedrock
    model_id: us.anthropic.claude-sonnet-5
    params:
      cache_prompt: default        # the system prompt
      cache_tools: default         # the tool list
      cache_config: {strategy: auto}   # the conversation up to the last message
      max_tokens: 32000
```

Three of Bedrock's four cache points: a tool loop re-reads its system prompt,
tools and earlier turns from the cache instead of paying for them again on
every call. Loaded history keeps a cache point of its own
([Multi-turn history](../workflows/multi-turn-history.md#loaded-memory-is-cached-on-bedrock)).
Anything that rewrites the start of the prompt on every call — a timestamp at
the top of a system prompt, say — defeats the cache; append such lines at the
end instead.

## 2. Think where it pays

```yaml
models:
  default:
    thinking: {effort: high, display: summarized}

agents:
  orchestrator:
    thinking: {effort: medium}     # routing and synthesis
  sql_analyst:
    thinking: false                # writes code against a known schema
```

Set thinking on the model as a default, then turn it down per agent
([Chapter 2](../configuration/Chapter_02.md#thinking)). Agents doing
mechanical work against clear instructions rarely gain from it; the agent that
plans and judges does. Adaptive thinking needs a model that supports it — a
config written for Sonnet 5 sent to an older model fails at the provider.

## 3. Clear old tool results

```yaml
agents:
  sql_analyst:
    context:
      clear_tool_results:
        trigger: 60000
        keep_last: 3
        keep_tools: [sql___get_schema]
```

Query results are the bulk of a long analysis, and an agent rarely needs the
ninth-from-last one verbatim. Past the trigger, older results are replaced by a
line naming the tool and the ids in the result, so the agent can fetch again
what it actually needs ([Chapter 8](../configuration/Chapter_08.md#clearing-old-tool-results)).
Keep reference results such as a schema.

## 4. Bound what carries across turns

```yaml
agents:
  orchestrator:
    conversation_manager:
      type: strands.agent:SlidingWindowConversationManager
      params: {window_size: 80}
    history:
      group: orchestrator
  sql_analyst:
    history:
      group: sql
      keep: masked
      summarize_over: 40000
      summarize_model: utility
```

A sliding window bounds what an agent keeps within a run. History bounds what
it carries into the next turn: masked history writes back each finished tool
result as a one-line pointer the agent can call again, and `summarize_over`
has a cheap model condense the oldest calls once the masked memory still grows
too large
([Multi-turn history](../workflows/multi-turn-history.md#keeping-a-masked-memory)).
Agents that share a history `group` share one memory — useful when two agents
take turns as the same voice.

## 5. Keep toolboxes small

Every tool's schema rides along on every call, and a model choosing among
forty tools chooses worse than one choosing among eight. Kaboo refuses a
workflow that gives an agent more than 20 tools, and splits each service's
tools into narrow MCP clients — an `_explore` client for read-only discovery
beside the full one — so an agent is granted exactly what its job needs
([Call MCP tools as the signed-in user](user-identity-mcp.md#relay-the-token-on-every-mcp-call)).

## 6. Put ceilings on runaway work

- `MaxToolCallsGuard` caps tool calls per invocation, so a confused agent stops
  instead of looping ([Chapter 6](../configuration/Chapter_06.md)).
- [Refusing repeated failures](guard-hooks.md#refuse-a-call-that-has-already-failed-twice)
  stops the commonest loop of all: the same broken call, again.
- A small, deterministic model for utility work — naming a conversation,
  classifying a request — costs a fraction of the analyst model:

    ```yaml
    models:
      utility:
        provider: bedrock
        model_id: us.anthropic.claude-haiku-4-5-20251001-v1:0
        params: {temperature: 0, max_tokens: 200}
    ```

## Measure it

Token usage per run arrives in the activity stream and in `TurnResult.usage_by_run`
in tests; with [telemetry](../evaluation.md#enable-telemetry) on, every model
call's tokens and latency land in your tracing backend. Measure before and
after each change — caching in particular is easy to break silently.
