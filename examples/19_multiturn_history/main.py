"""19_multiturn_history — Client-driven sub-agent memory.

assistant_team(delegate): coordinator -> memo(history: true). The `memo`
sub-agent's transcript round-trips via state.kaboo_history across turns.

The full round-trip (feeding a captured transcript back in) is a client concern,
best seen through the AG-UI server + kaboo-runtime persistence:

    OPENROUTER_API_KEY=... uv run kaboo-serve examples/19_multiturn_history/config.yaml

Usage (REPL):
    uv run python examples/19_multiturn_history/main.py
"""

from __future__ import annotations

from pathlib import Path

CONFIG = Path(__file__).parent / "config.yaml"
STARTER = "Remember that my favorite language is Rust."


def main() -> None:
    from kaboo_workflows import load

    resolved = load(CONFIG)
    entry = resolved.entry
    print(f"\n{52 * '-'}")
    print(f"Try: {STARTER}\n")
    print("coordinator -> memo (remembers across turns via history)")
    print("Type a message and press Enter. Empty line to exit.\n")
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
