"""End-to-end integration tests.

These drive a fully-resolved pipeline (YAML → build → wire → AG-UI adapter →
activity registry) through the *same* production code paths the FastAPI endpoint
uses, but with a deterministic :class:`~tests.fakes.ScriptedModel` instead of a
live LLM. Each test asserts both the AG-UI event stream and the resulting
hierarchical activity groups, across every supported orchestration composition
and their nested combinations.
"""
