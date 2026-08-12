"""Per-invocation LLM dollar-cost accumulation.

OpenRouter's usage accounting (``usage: {include: true}``) returns a ``cost``
field on every chat completion, but strands' ``Usage`` accumulation drops
unknown keys, so the value cannot ride ``EventLoopMetrics``. Instead the model
adapter (:class:`~kaboo_workflows.models_openai.KabooOpenAIModel`) deposits
each call's cost here, scoped to the innermost active agent invocation.

A ContextVar holds a *stack* of accumulator boxes rather than a single box so
nested invocations attribute correctly: when agent A delegates to agent B in
the same task context, B pushes its own box on start and pops it on complete,
after which A's later model calls land back in A's box. Parallel invocations
(swarm nodes in separate asyncio tasks) each see their own copy of the stack.
"""

from __future__ import annotations

from contextvars import ContextVar


class _CostBox:
    __slots__ = ("total",)

    def __init__(self) -> None:
        self.total = 0.0


_stack: ContextVar[tuple[_CostBox, ...]] = ContextVar("kaboo_cost_stack", default=())


def push_cost_box() -> None:
    """Open a fresh cost accumulator for the invocation that is starting."""
    _stack.set(_stack.get() + (_CostBox(),))


def pop_cost_box() -> float:
    """Close the innermost accumulator and return its total (0.0 if none)."""
    stack = _stack.get()
    if not stack:
        return 0.0
    _stack.set(stack[:-1])
    return stack[-1].total


def add_cost(amount: float) -> None:
    """Add a model call's cost to the innermost active accumulator, if any."""
    stack = _stack.get()
    if stack:
        stack[-1].total += amount
