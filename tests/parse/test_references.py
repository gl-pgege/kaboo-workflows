"""Parsing client references from a RunAgentInput.

Attachment references come from the latest user message's multimodal parts
(id/kind/name read from part ``metadata``, with a deterministic fallback);
object references come from ``state.kaboo_references``.
"""

from __future__ import annotations

from ag_ui.core import EventType as AGUIEventType
from ag_ui.core import (
    ImageInputContent,
    InputContentDataSource,
    InputContentUrlSource,
    MessagesSnapshotEvent,
    RunAgentInput,
    TextInputContent,
    UserMessage,
)

from kaboo_workflows.adapters.agui import _parse_references, _restore_snapshot_user_content


def _run_input(messages, state=None) -> RunAgentInput:
    return RunAgentInput(
        thread_id="t",
        run_id="r",
        state=state if state is not None else {},
        messages=messages,
        tools=[],
        context=[],
        forwarded_props={},
    )


def test_parses_attachment_part_metadata():
    msg = UserMessage(
        id="m1",
        content=[
            TextInputContent(text="look at this"),
            ImageInputContent(
                source=InputContentUrlSource(value="https://x/y.png", mime_type="image/png"),
                metadata={"kaboo_id": "img_1", "kaboo_kind": "file", "kaboo_name": "y.png"},
            ),
        ],
    )
    refs = _parse_references(_run_input([msg]))
    assert len(refs) == 1
    ref = refs[0]
    assert ref.id == "img_1"
    assert ref.kind == "file"
    assert ref.name == "y.png"
    assert ref.transport == "attachment"
    assert ref.source == "url"
    assert ref.value == "https://x/y.png"
    assert ref.mime_type == "image/png"


def test_deterministic_fallback_id_when_metadata_absent():
    msg = UserMessage(
        id="m2",
        content=[
            ImageInputContent(
                source=InputContentDataSource(value="AAAA", mime_type="image/png"),
            ),
        ],
    )
    refs = _parse_references(_run_input([msg]))
    assert refs[0].id == "m2:0"
    assert refs[0].kind == "image"


def test_parses_object_references_from_state():
    msg = UserMessage(id="m3", content="hi")
    state = {
        "kaboo_references": [
            {"kind": "table", "id": "t1", "name": "orders", "meta": {"schema": "public"}}
        ]
    }
    refs = _parse_references(_run_input([msg], state))
    assert len(refs) == 1
    assert refs[0].transport == "object"
    assert refs[0].id == "t1"
    assert refs[0].meta == {"schema": "public"}


def test_combines_attachments_and_objects():
    msg = UserMessage(
        id="m4",
        content=[
            ImageInputContent(
                source=InputContentDataSource(value="AAAA", mime_type="image/png"),
                metadata={"kaboo_id": "img_1", "kaboo_name": "y.png"},
            ),
        ],
    )
    state = {"kaboo_references": [{"kind": "table", "id": "t1", "name": "orders"}]}
    refs = _parse_references(_run_input([msg], state))
    assert {r.id for r in refs} == {"img_1", "t1"}


def test_plain_text_message_yields_no_attachment_refs():
    msg = UserMessage(id="m5", content="just text")
    assert _parse_references(_run_input([msg])) == []


def test_restore_snapshot_reinstates_structured_user_content():
    original = [
        TextInputContent(text="compare these"),
        ImageInputContent(
            source=InputContentUrlSource(value="https://x/y.png", mime_type="image/png"),
            metadata={"kaboo_id": "img_1", "kaboo_name": "y.png"},
        ),
    ]
    # ag-ui-strands emits the snapshot with the content stringified.
    event = MessagesSnapshotEvent(
        type=AGUIEventType.MESSAGES_SNAPSHOT,
        messages=[UserMessage(id="m1", content=str(original))],
    )
    _restore_snapshot_user_content(event, {"m1": original})
    assert event.messages[0].content == original


def test_restore_snapshot_ignores_unknown_ids():
    event = MessagesSnapshotEvent(
        type=AGUIEventType.MESSAGES_SNAPSHOT,
        messages=[UserMessage(id="other", content="unchanged")],
    )
    _restore_snapshot_user_content(event, {"m1": [TextInputContent(text="x")]})
    assert event.messages[0].content == "unchanged"
