"""21_runtime_configs — A different workflow on every run, no restart.

The service parses its base config once. Each run brings its own workflow as a
YAML string — from a database, an admin UI, a tenant record — and gets its own
agents, orchestration and MCP client sessions, released when the run ends.

Served over AG-UI, that is one parameter:

    app = create_agui_app("config.yaml", session_config_key="workflow_config")

and each request carries its config in `forwardedProps.workflow_config`. This
script shows the same two functions the endpoint uses, so you can see what the
overlay does before wiring a server.

Usage:
    uv run python examples/21_runtime_configs/main.py
"""

from __future__ import annotations

from pathlib import Path

CONFIG = Path(__file__).parent / "config.yaml"

# Two workflows a host might have stored per tenant, per agent type, or per
# customer. Neither declares `models:` — they name the one the base provides.
TRIAGE = """
agents:
  triage:
    model: default
    system_prompt: |
      You triage incoming reports. Reply with one line: the severity and why.

entry: triage
"""

RESEARCH_TEAM = """
agents:
  coordinator:
    model: default
    system_prompt: |
      You coordinate research. Delegate the question, then summarize the answer.
  librarian:
    model: default
    description: Finds and cites source material.
    system_prompt: |
      You answer with concrete sources and a one-line summary of each.

orchestrations:
  research_team:
    mode: delegate
    entry_name: coordinator
    connections:
      - { agent: librarian, description: "Looks up source material" }

entry: research_team
"""


def main() -> None:
    from kaboo_workflows import (
        load_session,
        load_session_config,
        parse_config_sources,
        resolve_infra,
    )

    # Boot: parse the shared layer once and keep the raw dict. Nothing is
    # connected, and this is safe to share across concurrent runs.
    base = parse_config_sources(str(CONFIG))

    for label, submitted in [
        ("no config submitted", None),
        ("triage", TRIAGE),
        ("research team", RESEARCH_TEAM),
    ]:
        # Per run: merge, validate, build. A bad config raises here and fails
        # this run only — the service keeps serving.
        config = load_session_config(base, submitted)
        infra = resolve_infra(config)
        resolved = load_session(config, infra, session_id=f"thread-{label}")
        try:
            print(f"\n{label}")
            print(f"  entry:  {config.entry}")
            print(f"  agents: {sorted(resolved.agents)}")
            print(f"  models: {sorted(config.models)}")
        finally:
            # In a server this is the run ending: the sessions it opened close
            # with it, so none can go stale before the next run.
            infra.mcp_lifecycle.stop()


if __name__ == "__main__":
    from kaboo_workflows import cli_errors

    with cli_errors():
        main()
