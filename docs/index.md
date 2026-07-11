# kaboo-workflows

**YAML-driven multi-agent orchestration with AG-UI and CopilotKit support, built
on [strands-agents](https://github.com/strands-agents/harness-sdk).**

Describe an entire multi-agent system in YAML — models, agents, tools, hooks, MCP
servers, and nested orchestrations — and `load()` hands back live, fully wired
strands objects. `kaboo-serve` then serves them as AG-UI Server-Sent Events that
any [CopilotKit](https://copilotkit.ai) frontend can consume.

## Start here

<div class="grid cards" markdown>

- **Quick start** — install, write a config, serve it. See the
  [README](https://github.com/gl-pgege/kaboo-workflows#quick-start).
- **[Configuration guide](configuration/README.md)** — the complete 18-chapter
  reference for every YAML option.
- **[Workflow guides](workflows/deep-nesting.md)** — worked, tested examples of
  deep nesting, swarm+graph, parallelism, HITL, history, and error handling.
- **[API reference](api-reference.md)** — the full public Python surface,
  auto-generated from docstrings.

</div>

## The kaboo stack

kaboo-workflows is the orchestration engine. It pairs with:

- **[kaboo-runtime](https://github.com/gl-pgege/kaboo-runtime)** — a CopilotKit
  runtime plugin for persisting and replaying agent event logs (in-memory or
  Postgres `ThreadStore`s).
- **[kaboo-react](https://github.com/gl-pgege/kaboo-react)** — React components
  for rendering agent activity.
- **[kaboo-workflows-demo](https://github.com/gl-pgege/kaboo-workflows-demo)** —
  a runnable, end-to-end reference wiring all three together.

See [the kaboo stack](https://gl-pgege.github.io/kaboo-docs/) for the whole
picture.

## Everything is proven

Nothing documented here ships without a test: every example config is
load-validated, every complex workflow guide is backed by a passing end-to-end
test, executable doc snippets run in CI, and a completeness gate guarantees every
public symbol has a docstring and an API page.
