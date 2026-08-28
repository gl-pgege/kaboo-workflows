"""18_hitl — Human-in-the-loop, nested + parallel gates.

review_team(delegate): coordinator -> field_agent(interrupt: true). The gated
agent's `ask_user` interrupt bubbles to the single top-level handler.

Note: driving HITL from a terminal REPL is limited (there is no resume UI). This
flow is best experienced through the AG-UI server + a CopilotKit frontend:

    uv run kaboo-serve examples/18_hitl/config.yaml

Usage (REPL, first turn only):
    uv run python examples/18_hitl/main.py
"""

from __future__ import annotations

from pathlib import Path

CONFIG = Path(__file__).parent / "config.yaml"
STARTER = "Archive all inactive customer accounts from last year."


def main() -> None:
    from kaboo_workflows import load

    resolved = load(CONFIG)
    entry = resolved.entry
    print(f"\n{52 * '-'}")
    print(f"Try: {STARTER}\n")
    print("coordinator -> field_agent (asks the user before acting)")
    print("For the full resume flow, serve it: uv run kaboo-serve examples/18_hitl/config.yaml\n")
    try:
        while True:
            msg = input("You: ").strip()
            if not msg:
                break
            print()
            entry(msg)
            print("\n" + 52 * "-" + "\n")
    except KeyboardInterrupt:
        print("\nGoodbye!")
    finally:
        resolved.mcp_lifecycle.stop()


if __name__ == "__main__":
    from kaboo_workflows import cli_errors

    with cli_errors():
        main()
