"""Apply the entry agent's conversation manager when its run starts.

ag-ui-strands rebuilds the entry agent's messages from the chat on every run,
so a window its conversation manager applies after a run never reaches the next
one, and the agent's input grows with the chat. Applying it once as the run
starts keeps the window it declares, and leaves the messages alone for the rest
of the run, so a prompt cache over them keeps applying.
"""

from __future__ import annotations

from typing import Any, override

from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import BeforeInvocationEvent


class WindowOnStart(HookProvider):
    @override
    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self._apply)

    def _apply(self, event: BeforeInvocationEvent) -> None:
        manager = getattr(event.agent, "conversation_manager", None)
        if manager is not None:
            manager.apply_management(event.agent)
