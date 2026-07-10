"""Stable per-turn id resolution.

A turn is one user message and everything it produces — including work that
first appears only after an interrupt/resume. The resume POST carries a fresh
``run_id``, so ``run_id`` cannot identify the turn. ``_resolve_turn_id`` mints an
id on each non-resume POST and reuses it for resumes of the same thread, so the
UI can bind every group of a turn (pre- and post-resume) to that turn's reply.
"""

from __future__ import annotations

from kaboo_workflows.adapters import agui as agui_mod
from kaboo_workflows.adapters.agui import _resolve_turn_id


def _reset() -> None:
    agui_mod._turn_by_thread.clear()


def test_new_turn_uses_run_id_and_persists() -> None:
    _reset()
    turn = _resolve_turn_id("t1", "run-A", is_resume=False)
    assert turn == "run-A"
    assert agui_mod._turn_by_thread["t1"] == "run-A"


def test_resume_reuses_the_turn_across_a_new_run_id() -> None:
    _reset()
    first = _resolve_turn_id("t1", "run-A", is_resume=False)
    # The resume POST has a different run_id but continues the same turn.
    resumed = _resolve_turn_id("t1", "run-B", is_resume=True)
    assert resumed == first == "run-A"


def test_next_user_turn_starts_a_fresh_turn_id() -> None:
    _reset()
    t1 = _resolve_turn_id("t1", "run-A", is_resume=False)
    _resolve_turn_id("t1", "run-B", is_resume=True)
    t2 = _resolve_turn_id("t1", "run-C", is_resume=False)
    assert t2 == "run-C"
    assert t2 != t1


def test_threads_are_isolated() -> None:
    _reset()
    a = _resolve_turn_id("t1", "run-A", is_resume=False)
    b = _resolve_turn_id("t2", "run-B", is_resume=False)
    assert a != b
    assert _resolve_turn_id("t1", "run-C", is_resume=True) == a
    assert _resolve_turn_id("t2", "run-D", is_resume=True) == b


def test_resume_without_a_known_turn_falls_back_to_run_id() -> None:
    # e.g. process restarted mid-turn: no stored id, so use this run's id.
    _reset()
    assert _resolve_turn_id("t1", "run-X", is_resume=True) == "run-X"
