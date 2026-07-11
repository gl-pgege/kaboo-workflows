"""20_error_and_rejection — When things go wrong.

review_swarm(swarm): planner -> researcher -> writer. Demonstrates error
isolation (a failing node terminates the run cleanly, isolated to that node) and
the rejection path (declining a gated tool resumes to a clean finish).

Usage:
    uv run python examples/20_error_and_rejection/main.py
"""

from __future__ import annotations

from pathlib import Path

CONFIG = Path(__file__).parent / "config.yaml"
STARTER = "Summarize our Q1 incident reports."


def main() -> None:
    from kaboo_workflows import load

    resolved = load(CONFIG)
    entry = resolved.entry
    print(f"\n{52 * '-'}")
    print(f"Try: {STARTER}\n")
    print("planner -> researcher -> writer (errors stay isolated; the run never hangs)")
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
