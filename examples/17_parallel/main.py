"""17_parallel — Parallel top-level + nested parallelism.

pipeline(graph): strategist -> [writer, subteam] -> editor, where subteam is
itself a graph that fans out to two analysts.

Usage:
    uv run python examples/17_parallel/main.py
"""

from __future__ import annotations

from pathlib import Path

CONFIG = Path(__file__).parent / "config.yaml"
STARTER = "Write a briefing on the impact of RISC-V on the laptop market."


def main() -> None:
    from kaboo_workflows import load

    resolved = load(CONFIG)
    entry = resolved.entry
    print(f"\n{52 * '-'}")
    print(f"Try: {STARTER}\n")
    print("strategist -> [writer, subteam(analyst_a || analyst_b)] -> editor")
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
