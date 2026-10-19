"""A tool that yields to the event loop, so work a test runs at once really overlaps."""

import asyncio

from strands import tool


@tool
async def look_up(topic: str) -> str:
    """Look a topic up, taking a moment as a real lookup would."""
    await asyncio.sleep(0.05)
    return f"notes on {topic}"
