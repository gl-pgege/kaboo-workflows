"""OpenAI-compatible model with OpenRouter cost capture.

Requires the ``openai`` extra; import only from the ``create_model`` openai
branch.
"""

from __future__ import annotations

from typing import Any

from strands.models.openai import OpenAIModel
from strands.types.streaming import StreamEvent

from .hooks.cost_tracking import add_cost


class KabooOpenAIModel(OpenAIModel):
    """OpenAIModel that records per-call dollar cost from usage accounting.

    When the request opts into OpenRouter usage accounting
    (``extra_body: {usage: {include: true}}``), the response usage carries a
    ``cost`` field that the OpenAI SDK exposes via ``model_extra``. Strands'
    usage mapping drops it, so it is deposited into the per-invocation cost
    accumulator (see :mod:`kaboo_workflows.hooks.cost_tracking`) as each
    metadata chunk is formatted. Covers both streaming and non-streaming
    responses (the non-streaming path funnels through ``format_chunk`` too).
    """

    def format_chunk(self, event: dict[str, Any], **kwargs: Any) -> StreamEvent:
        if event.get("chunk_type") == "metadata":
            data = event.get("data")
            cost = getattr(data, "cost", None)
            if cost is None:
                extra = getattr(data, "model_extra", None) or {}
                cost = extra.get("cost")
            if cost:
                try:
                    add_cost(float(cost))
                except (TypeError, ValueError):
                    pass
        return super().format_chunk(event, **kwargs)
