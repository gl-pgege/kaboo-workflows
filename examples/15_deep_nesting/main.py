"""15_deep_nesting — Delegation, 3 levels deep.

research_pipeline(coordinator) -> research_team(lead) -> field_team(field_researcher -> verifier).
A single top-level entry drives a 3-deep delegate chain.

Usage:
    uv run python examples/15_deep_nesting/main.py
"""

from __future__ import annotations

from pathlib import Path

CONFIG = Path(__file__).parent / "config.yaml"
STARTER = "Research the state of on-device AI inference in 2026."


def main() -> None:
    from kaboo_workflows import load

    resolved = load(CONFIG)
    entry = resolved.entry
    print(f"\n{52 * '-'}")
    print(f"Try: {STARTER}\n")
    print("coordinator -> research_team(lead) -> field_team(field_researcher -> verifier)")
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
