# Compose a workflow per run

One service can run a different workflow on every request: a workflow per
tenant, per project, per row an admin edits in a UI. Nothing is restarted and
nothing is read from disk at run time. The host stores workflows, composes the
final YAML for each run, and submits it with the run; the agent service layers
it over its own base config
([Chapter 17](../configuration/Chapter_17.md#when-the-config-itself-is-per-request),
`examples/21_runtime_configs/`).

## Split the config in two

**The base config** lives with the service and holds what every workflow
shares, including anything secret:

```yaml
# config.yaml — the service's own file
models:
  analyst-model:
    provider: bedrock
    model_id: ${BEDROCK_MODEL_ID:-us.anthropic.claude-sonnet-5}
    params: {region_name: "${BEDROCK_REGION:-us-east-1}"}

mcp_clients:
  sql:
    url: ${GATEWAY_URL}
    transport: streamable-http
    auth: {type: relay}
    params:
      tool_filters: {allowed: [sql___run_query, sql___get_schema]}

attachments:
  default: reference

agents:
  assistant:                       # the fallback when a run submits nothing
    model: analyst-model
    system_prompt: You are a helpful data assistant.
entry: assistant
```

**A stored workflow** holds only `agents`, `orchestrations` and `entry`, and
refers to models and MCP clients by name:

```yaml
agents:
  analyst:
    model: analyst-model
    mcp: [sql]
    system_prompt: You answer questions from the sales database.
entry: analyst
```

The overlay replaces the base's `agents`, `orchestrations` and `entry`, adds to
its `models` and `mcp_clients`, and may not declare `mcp_servers` at all — a run
may not launch a process. Stored workflows that only *name* the base's models
and clients never carry a URL or a credential.

## Compose, then submit

Composition is plain dictionary work on the host. Ours applies the user's
choices (thinking level, model override), drops agents for data sources the run
does not have, inlines [skills](skills.md), adds the
[hooks every agent needs](guard-hooks.md), stamps today's date, and fills in
IDs such as the current project:

```python
import yaml

from kaboo_workflows import load_session_config, parse_config_sources

EVERY_AGENT_HOOKS = ["my_service.hooks:AnswerEveryToolUse"]


def compose(stored: str, *, project_id: str) -> str:
    doc = yaml.safe_load(stored)
    for agent in doc.get("agents", {}).values():
        agent["hooks"] = [*agent.get("hooks", []), *EVERY_AGENT_HOOKS]
    text = yaml.safe_dump(doc, sort_keys=False)
    text = text.replace("{{project_id}}", project_id)
    return text.replace("${", "$${")


STORED = """
agents:
  analyst:
    model: analyst-model
    system_prompt: |
      You answer questions about project {{project_id}}.
      Quote templates such as ${name} literally.
entry: analyst
"""

BASE = parse_config_sources("""
models:
  analyst-model: {provider: bedrock, model_id: us.anthropic.claude-sonnet-5}
agents:
  assistant: {model: analyst-model, system_prompt: Fallback.}
entry: assistant
""")

composed = compose(STORED, project_id="p-42")
config = load_session_config(BASE, composed)

prompt = config.agents["analyst"].system_prompt
assert "project p-42" in prompt
assert "${name}" in prompt
assert config.agents["analyst"].hooks == EVERY_AGENT_HOOKS
```

The run then carries the composed text in `forwardedProps`, under the key the
service was started with (`create_agui_app(session_config_key="workflow_config")`):

```json
{
  "threadId": "t-1",
  "runId": "r-1",
  "messages": [{"id": "m-1", "role": "user", "content": "How did Q3 go?"}],
  "forwardedProps": {"workflow_config": "agents:\n  analyst: ..."}
}
```

## Escape interpolation in anything authored outside the repository

A submitted workflow is interpolated against **the service's environment**, on
purpose, so an admin-authored config can say `${API_KEY}` without the secret
living in the database. The flip side: any `${…}` in user-authored text — a
prompt that quotes template syntax, or one written to fish for
`${AWS_SECRET_ACCESS_KEY}` — resolves against the server too.

`$${` is the escape for a literal `${`. We escape every `${` in the composed
workflow, which means stored workflows never read the environment and
everything secret stays in the base config. If your workflow authors are
trusted and need environment values, escape only the fields they write freely,
such as prompts.

## Keep it safe and testable

- **Validate on save**, with the same loader a run uses
  ([production recipe](production-serving.md#validate-a-workflow-before-saving-it)),
  so a broken workflow is an error in the editor rather than a failed run.
- **Pass `allowed_mcp_hosts`** to `create_agui_app` and `load_session_config`
  if a workflow can declare its own MCP clients.
- **Refuse leftovers.** After filling placeholders, fail composition if any
  remain; a prompt that still says `{{dashboard_id}}` produces confident
  nonsense.
- **Test the composed text**, not the stored one:
  `Pipeline(base, session_config_key="workflow_config")` with the composer's
  output in `forwarded_props` runs it end to end
  ([Testing](../testing.md#runtime-composed-workflows)).
