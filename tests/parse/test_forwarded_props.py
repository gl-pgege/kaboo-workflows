"""forwarded_props binding + attachment-kind object reference upgrade."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from ag_ui.core import RunAgentInput

from kaboo_workflows._context import get_forwarded_props, set_forwarded_props
from kaboo_workflows.adapters.agui import _parse_object_references
from kaboo_workflows.tools.fetching import set_attachment_url_template


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    set_forwarded_props(None)
    set_attachment_url_template(None)
    yield
    set_forwarded_props(None)
    set_attachment_url_template(None)


def test_forwarded_props_roundtrip_and_coercion():
    assert get_forwarded_props() == {}
    set_forwarded_props({"runToken": "t", "agent_config": {"model_id": "m"}})
    assert get_forwarded_props()["runToken"] == "t"
    set_forwarded_props(None)
    assert get_forwarded_props() == {}
    set_forwarded_props("not-a-dict")  # ty: ignore[invalid-argument-type]
    assert get_forwarded_props() == {}


def _run_input(state: dict) -> RunAgentInput:
    return RunAgentInput(
        thread_id="t",
        run_id="r",
        state=state,
        messages=[],
        tools=[],
        context=[],
        forwarded_props={},
    )


def _refs(state_entries: list[dict]) -> list:
    return _parse_object_references(_run_input({"kaboo_references": state_entries}))


def test_object_reference_stays_object():
    (ref,) = _refs([{"kind": "table", "id": "t1", "name": "orders"}])
    assert ref.transport == "object"
    assert ref.value is None


def test_attachment_with_meta_url_upgrades():
    (ref,) = _refs(
        [
            {
                "kind": "attachment",
                "id": "a1",
                "name": "report.pdf",
                "meta": {"url": "/attachments/a1/content", "mimeType": "application/pdf"},
            }
        ]
    )
    assert ref.transport == "attachment"
    assert ref.source == "url"
    assert ref.value == "/attachments/a1/content"
    assert ref.mime_type == "application/pdf"


def test_attachment_without_url_uses_template():
    set_attachment_url_template("/attachments/{id}/content")
    (ref,) = _refs([{"kind": "attachment", "id": "a2", "name": "license.pdf"}])
    assert ref.transport == "attachment"
    assert ref.value == "/attachments/a2/content"
    # MIME guessed from the file name when meta has none.
    assert ref.mime_type == "application/pdf"


def test_attachment_without_url_or_template_stays_object():
    (ref,) = _refs([{"kind": "attachment", "id": "a3", "name": "x.pdf"}])
    assert ref.transport == "object"
    assert ref.value is None
