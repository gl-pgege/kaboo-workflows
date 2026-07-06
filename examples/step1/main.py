"""step1 — Minimal Agent (REPL mode).

Interactive REPL for testing the agent locally without starting a server.
For AG-UI SSE serving, use: kaboo-serve examples/step1/config.yaml

Usage:
    OPENROUTER_API_KEY=... uv run python examples/step1/main.py
"""

from __future__ import annotations

from pathlib import Path

CONFIG = Path(__file__).parent / "config.yaml"
STARTER = "What is 15 * 23?"


def main() -> None:
    from kaboo_workflows import load

    resolved = load(CONFIG)
    agent = resolved.entry
    print(f"\n{52 * '-'}")
    print(f"Try: {STARTER}\n")
    print("Type a message and press Enter. Empty line to exit.\n")
    try:
        while True:
            msg = input("You: ").strip()
            if not msg:
                break
            print()
            agent(msg)
            print("\n" + 52 * "-" + "\n")
    except KeyboardInterrupt:
        print("\nGoodbye!")
    finally:
        resolved.mcp_lifecycle.stop()


if __name__ == "__main__":
    from kaboo_workflows import cli_errors

    with cli_errors():
        main()
