"""Test your own workflows end to end, with no model provider or network.

:class:`ScriptedModel` stands in for an LLM with a fixed per-agent script, and
:class:`Pipeline` drives a config through the same AG-UI path a deployed
service uses, returning what a client would see as a :class:`TurnResult`.
"""

from __future__ import annotations

from .pipeline import Pipeline, TurnResult, run_pipeline
from .scripted_model import ScriptedModel

__all__ = ["Pipeline", "ScriptedModel", "TurnResult", "run_pipeline"]
