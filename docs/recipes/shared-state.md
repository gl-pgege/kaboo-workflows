# Shared state: state effects and workspace views

A conversation builds things — dashboards, saved queries, a report — and the
person and every agent need to know what exists without asking for it. Two
pieces of config keep that in the AG-UI state the conversation already carries:

- **State effects** write a tool's result into the state the moment the call
  succeeds. The client receives each change as a `STATE_DELTA`, so a canvas can
  follow the agent while it works.
- **Workspace views** show an agent parts of that state at every model call, so
  it extends what was built instead of listing and reading it first.

## Write what a tool did

```yaml
state_effects:
  - tool: sql-workbench___create_queries
    mapper: my_service.effects:queries_saved
  - tool: "dashboard___*_widgets"          # an fnmatch glob
    mapper: my_service.effects:dashboard_changed
  - tool: project___get_context
    path: /kaboo_workspace/project          # the parsed result, written whole
```

An effect names a tool, or a glob of names, and either a `path` to write the
tool's parsed result to, or a `mapper`. A mapper receives a
`StateEffectCall` — the tool, its input, its result (the tool's MCP
`structuredContent` when it sends one, otherwise its text, parsed as JSON when it
is JSON), the agent, the run and the state before the effect — and returns JSON
Patch operations. A tool can therefore answer the model in prose and the mapper
in structured data:

```python
from typing import Any

from kaboo_workflows.hooks import StateEffectCall

ROOT = "/kaboo_workspace"


def queries_saved(call: StateEffectCall) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    if "kaboo_workspace" not in call.state:
        operations.append({"op": "add", "path": ROOT, "value": {"queries": {}, "changes": []}})
    for query in (call.result or {}).get("created", []):
        operations.append(
            {"op": "add", "path": f"{ROOT}/queries/{query['query_id']}", "value": {"title": query["title"]}}
        )
    operations.append(
        {"op": "add", "path": f"{ROOT}/changes/-", "value": {"agent": call.agent, "summary": "Saved queries"}}
    )
    return operations
```

- **Effects run for every agent**, delegates included, after a call succeeds. A
  failed call writes nothing.
- **Keep state small and by reference.** Write ids, titles and what changed,
  never data rows: the state travels with every run, and widgets fetch their
  own rows by id.
- **One writer per key.** The agent's effects own `kaboo_workspace`; the
  person's own edits go to the services, and the agent sees them through
  context or the next effect. kaboo-runtime keeps server-owned keys away from
  the browser ([state ownership](https://gl-pgege.github.io/kaboo-runtime/replay-and-state/)).

## Show it to an agent

```yaml
agents:
  analyst:
    workspace: [/kaboo_workspace]
  visualizer:
    workspace: [/kaboo_workspace/dashboards]
```

`workspace` lists JSON Pointers into the state. Before every model call, delegates
included, the agent's latest message opens with what they point at, current as
of that call:

```text
The workspace — what this conversation has built, current as of now:
/kaboo_workspace: {"dashboards":{"d_2":{"title":"Churn by region","version":3}},...}
```

The agent's own conversation is untouched; only what reaches its model changes.
Point each agent at what it needs: a visualizer needs the dashboards, not every
saved query.

## Show what a service holds: working views

The workspace holds what the conversation noted. When the thing itself lives in a
service — the dashboards an agent is building, with every widget and who changed
what — a working view asks the service:

```yaml
agents:
  analyst:
    views: [my_service.views:dashboards]
```

A view is a callable that takes a `ViewRequest` (the agent, the thread and run
ids, whether this is the invocation's first model call, and the run's AG-UI
context) and returns a `View` or `None`:

```python
from kaboo_workflows.hooks import View, ViewRequest


def dashboards(request: ViewRequest) -> View | None:
    if request.first_call:
        return View(base=render_dashboards(request.thread_id))
    return View(changes=changes_since_base(request.thread_id, request.agent))
```

- **The base** is kept from the invocation's first model call and opens the
  latest human message with the run's context, so it stays put for the whole
  invocation and a prompt cache over the conversation keeps applying.
- **The changes** are rendered at every model call and end the latest message,
  after the tool results it follows: what the person or other agents changed
  since the base, so the agent keeps their edits without reading the service
  again.
- A view that raises is left out of that call and logged; the run goes on.

## What it saves

In the lab, extending a dashboard the conversation built takes the agent two
tool calls with a workspace view and four without, the extra two being lookups
(`dashboard___list_dashboards`, `dashboard___get_dashboard`) that only re-read
what the state already held. Run both sides with
`yarn lab compare workspace-vs-read --live` for a real model's numbers.

## Test it

`TurnResult.state()` applies every `STATE_DELTA` the run sent, so a test can
assert what the workspace holds after a turn. Lab scenarios:
[workspace-extend](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/workspace-extend.yaml),
[workspace-extend-read](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/workspace-extend-read.yaml),
[workspace-refresh](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/workspace-refresh.yaml)
and
[delegate-dashboard](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/delegate-dashboard.yaml).
