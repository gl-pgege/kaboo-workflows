"""Built-in reference tools + inline resolver.

``list_references`` / ``fetch_attachment`` read the request-scoped reference
registry; ``resolve_inline_blocks`` materializes supported media. Uses base64
``data`` sources so no network is touched.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator

import pytest

from kaboo_workflows._context import (
    Reference,
    set_references,
)
from kaboo_workflows.tools.references import (
    fetch_attachment,
    list_references,
    resolve_inline_blocks,
)


@pytest.fixture(autouse=True)
def _isolate_refs() -> Iterator[None]:
    set_references(None)
    yield
    set_references(None)


def _data(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _call(tool, **kwargs) -> dict:
    """Invoke a strands @tool via its underlying function."""
    fn = getattr(tool, "original_function", None) or getattr(tool, "_tool_func", None) or tool
    return fn(**kwargs)


def test_list_references_empty():
    set_references([])
    out = _call(list_references)
    assert "No references" in out["content"][0]["text"]


def test_list_references_lists_all():
    set_references(
        [
            Reference(
                kind="file", id="f1", name="a.txt", transport="attachment", mime_type="text/plain"
            ),
            Reference(kind="table", id="t1", name="orders", transport="object"),
        ]
    )
    text = _call(list_references)["content"][0]["text"]
    assert "a.txt" in text and "f1" in text
    assert "orders" in text and "t1" in text


def test_fetch_text_attachment_returns_decoded_text():
    set_references(
        [
            Reference(
                kind="file",
                id="f1",
                name="a.txt",
                transport="attachment",
                mime_type="text/plain",
                source="data",
                value=_data(b"file body"),
            )
        ]
    )
    out = _call(fetch_attachment, reference_id="f1")
    assert out["status"] == "success"
    assert out["content"][0]["text"] == "file body"


def test_fetch_media_attachment_returns_the_media_in_its_result():
    set_references(
        [
            Reference(
                kind="file",
                id="img",
                name="a.png",
                transport="attachment",
                mime_type="image/png",
                source="data",
                value=_data(b"\x89PNG"),
            )
        ]
    )
    out = _call(fetch_attachment, reference_id="img")
    assert out["status"] == "success"
    assert any(block.get("image", {}).get("format") == "png" for block in out["content"])


def test_fetch_unknown_id_errors():
    set_references([])
    out = _call(fetch_attachment, reference_id="nope")
    assert out["status"] == "error"


def test_fetch_object_reference_is_rejected():
    set_references([Reference(kind="table", id="t1", name="orders", transport="object")])
    out = _call(fetch_attachment, reference_id="t1")
    assert out["status"] == "error"
    assert "not a file" in out["content"][0]["text"]


def test_resolve_inline_blocks_skips_objects_and_unsupported():
    set_references(None)
    refs = [
        Reference(
            kind="file",
            id="img",
            name="a.png",
            transport="attachment",
            mime_type="image/png",
            source="data",
            value=_data(b"\x89PNG"),
        ),
        Reference(kind="table", id="t1", name="orders", transport="object"),
        Reference(
            kind="file",
            id="aud",
            name="a.mp3",
            transport="attachment",
            mime_type="audio/mpeg",
            source="data",
            value=_data(b"ID3"),
        ),
    ]
    blocks = resolve_inline_blocks(refs)
    assert len(blocks) == 1
    assert "image" in blocks[0]
