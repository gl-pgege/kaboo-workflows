"""Stream-group ``#N`` suffixing when one agent re-activates within a turn.

A swarm member (or any node) can run more than once in a single turn — swarms
hand back and forth, e.g. ``planner -> writer -> planner -> writer``. Each fresh
activation must get its own addressable stream group (``planner``, ``planner#2``,
...) under the *same* parent, so the UI renders each pass as a distinct card and
never overwrites the earlier one. A resume (interrupt continuation) is NOT a new
activation: it reuses the existing group and emits no group-resetting start.
"""

from __future__ import annotations

from types import SimpleNamespace

from kaboo_workflows._context import set_activity_context
from kaboo_workflows.hooks import EventPublisher
from kaboo_workflows.types import EventType


def _publisher() -> tuple[EventPublisher, list]:
    events: list = []
    pub = EventPublisher(
        callback=events.append,
        agent_name="planner",
        stream_group="research_pipeline.research_swarm.planner",
        stream_title="Planning (swarm)",
    )
    return pub, events


def _start_event(*, resuming: bool = False) -> SimpleNamespace:
    interrupt_state = SimpleNamespace(activated=True) if resuming else None
    agent = SimpleNamespace(_interrupt_state=interrupt_state, messages=[])
    return SimpleNamespace(
        agent=agent,
        messages=[{"role": "user", "content": [{"text": "do the work"}]}],
    )


def _group_starts(events: list) -> list:
    return [e for e in events if e.type == EventType.STREAM_GROUP_START]


def test_reactivation_gets_a_distinct_numbered_group_under_same_parent() -> None:
    set_activity_context("t1", "r1", "turn1")
    pub, events = _publisher()

    pub._on_agent_start(_start_event())
    pub._on_agent_start(_start_event())
    pub._on_agent_start(_start_event())

    starts = _group_starts(events)
    groups = [e.data["stream_group"] for e in starts]
    assert groups == [
        "research_pipeline.research_swarm.planner",
        "research_pipeline.research_swarm.planner#2",
        "research_pipeline.research_swarm.planner#3",
    ]
    # Every pass keeps the same parent, so all cards nest under the swarm node.
    parents = {e.data["parent_group"] for e in starts}
    assert parents == {"research_pipeline.research_swarm"}


def test_resume_reuses_the_group_and_emits_no_new_start() -> None:
    set_activity_context("t1", "r1", "turn1")
    pub, events = _publisher()

    pub._on_agent_start(_start_event())  # first real activation
    events.clear()

    pub._on_agent_start(_start_event(resuming=True))  # interrupt continuation

    # A resume is a continuation, not a new activation: no group-resetting start.
    assert _group_starts(events) == []
    assert any(e.type == EventType.AGENT_START for e in events)


def test_counters_are_isolated_per_thread() -> None:
    pub, events = _publisher()

    set_activity_context("tA", "r1", "turnA")
    pub._on_agent_start(_start_event())
    set_activity_context("tB", "r2", "turnB")
    pub._on_agent_start(_start_event())

    # Each thread's first activation is the bare group (no cross-thread #2).
    groups = [e.data["stream_group"] for e in _group_starts(events)]
    assert groups == [
        "research_pipeline.research_swarm.planner",
        "research_pipeline.research_swarm.planner",
    ]
