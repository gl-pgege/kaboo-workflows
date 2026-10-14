"""ag-ui-strands' RAW-forwarding warning stays quiet for kaboo's own interrupts."""

from __future__ import annotations

import logging

import pytest

import kaboo_workflows.adapters.agui  # noqa: F401


def test_the_raw_warning_for_an_interrupt_event_is_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    log = logging.getLogger("ag_ui_strands.agent")
    with caplog.at_level(logging.WARNING, logger="ag_ui_strands.agent"):
        log.warning(
            "Dropping unserializable Strands event from RAW forwarding (keys=['tool_interrupt_event']): x"
        )
        log.warning("Dropping unserializable Strands event from RAW forwarding (keys=['other']): y")

    assert [r.getMessage() for r in caplog.records] == [
        "Dropping unserializable Strands event from RAW forwarding (keys=['other']): y"
    ]
