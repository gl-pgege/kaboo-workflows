"""Built-in reference tools and inline media resolution.

These are the *generic* helpers the library ships so any in-scope agent can act
on a file attachment the user referenced:

- :func:`list_references` — enumerate every reference this run (any kind).
- :func:`fetch_attachment` — resolve one **file** attachment to text (for text-
  like types) or an inlined media ``ContentBlock`` (for images/documents/video).

Custom object kinds (tables, dashboards, …) are deliberately **not** resolvable
here — they are pointers the app resolves with its own MCP tool. There is no
generic per-kind resolver; the manifest hands the model the ``kind``+``id`` and
the app supplies the tool.

:func:`resolve_inline_blocks` is used by
:class:`~kaboo_workflows.hooks.reference_hook.ReferenceHook` to prepend resolved
media for ``inline`` agents.
"""

from __future__ import annotations

import base64
import logging
import urllib.parse
import urllib.request
from typing import Any

from strands import tool

from .._context import Reference, get_references, request_inline

logger = logging.getLogger(__name__)

# Format allowlists mirror ag-ui-strands so inline behavior is consistent with
# what the entry agent already receives.
_IMAGE_FORMATS = {"png", "jpeg", "gif", "webp"}
_DOCUMENT_FORMATS = {"pdf", "csv", "doc", "docx", "xls", "xlsx", "html", "txt", "md"}
_VIDEO_FORMATS = {"flv", "mkv", "mov", "mpeg", "mpg", "mp4", "three_gp", "webm", "wmv"}
_TEXT_FORMATS = {"txt", "md", "csv", "html", "json", "xml"}

# Guard: server-side URL fetches inline bytes with no auth (presigned/public
# URLs only). Cap the size so a hostile/huge attachment can't exhaust memory.
_MAX_FETCH_BYTES = 25 * 1024 * 1024


_TEXT_MIME_TYPES = {"application/json", "application/xml", "application/x-yaml", "application/yaml"}


def _mime_to_format(mime_type: str | None, allowed: set[str]) -> str | None:
    """Parse a MIME type to a short format, or ``None`` when unsupported/absent."""
    if not mime_type:
        return None
    fmt = mime_type.rsplit("/", 1)[-1].lower()
    return fmt if fmt in allowed else None


def _is_text_mime(mime_type: str | None) -> bool:
    """Whether a MIME type is best returned as decoded text."""
    if not mime_type:
        return False
    mime = mime_type.lower()
    if mime.startswith("text/"):
        return True
    return mime in _TEXT_MIME_TYPES or _mime_to_format(mime, _TEXT_FORMATS) is not None


def _fetch_url_bytes(url: str) -> bytes | None:
    """Fetch bytes from a presigned/public URL (no auth), size-capped."""
    # Only http(s): reject file:/ and custom schemes (local-file read / SSRF).
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        logger.warning("refusing to fetch reference url with non-http(s) scheme")
        return None
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310  # nosec B310
            data = resp.read(_MAX_FETCH_BYTES + 1)
    except Exception as exc:
        logger.warning("failed to fetch reference url: %s", exc)
        return None
    if len(data) > _MAX_FETCH_BYTES:
        logger.warning("reference exceeds %d-byte fetch cap; skipping", _MAX_FETCH_BYTES)
        return None
    return data


def _resolve_bytes(ref: Reference) -> bytes | None:
    """Resolve an attachment reference to raw bytes (base64 data or URL fetch)."""
    if ref.source == "data" and ref.value is not None:
        try:
            return base64.b64decode(ref.value)
        except Exception as exc:
            logger.warning("failed to decode base64 reference %s: %s", ref.id, exc)
            return None
    if ref.source == "url" and ref.value is not None:
        return _fetch_url_bytes(ref.value)
    return None


def _content_block(ref: Reference, raw: bytes) -> dict[str, Any] | None:
    """Build a strands media ``ContentBlock`` for a supported attachment.

    Gated by the MIME *top-level* type so e.g. ``audio/mpeg`` never matches a
    video format merely because ``mpeg`` is a video subtype token.
    """
    mime = (ref.mime_type or "").lower()
    top = mime.split("/", 1)[0]
    if top == "image":
        fmt = _mime_to_format(mime, _IMAGE_FORMATS)
        if fmt:
            return {"image": {"format": fmt, "source": {"bytes": raw}}}
    elif top == "video":
        fmt = _mime_to_format(mime, _VIDEO_FORMATS)
        if fmt:
            return {"video": {"format": fmt, "source": {"bytes": raw}}}
    elif top in ("application", "text"):
        fmt = _mime_to_format(mime, _DOCUMENT_FORMATS)
        if fmt:
            return {
                "document": {
                    "format": fmt,
                    "name": ref.name or "document",
                    "source": {"bytes": raw},
                }
            }
    return None


def resolve_inline_blocks(references: list[Reference]) -> list[dict[str, Any]]:
    """Resolve attachment references to inline media ``ContentBlock`` dicts.

    Only blob-capable attachment references with a supported image/document/
    video type are materialized; object references, audio, and unknown/absent
    MIME types are skipped (matching ag-ui-strands' behavior for the entry
    agent).
    """
    blocks: list[dict[str, Any]] = []
    for ref in references:
        if ref.transport != "attachment":
            continue
        raw = _resolve_bytes(ref)
        if raw is None:
            continue
        block = _content_block(ref, raw)
        if block is not None:
            blocks.append(block)
    return blocks


def _find(reference_id: str) -> Reference | None:
    for ref in get_references():
        if ref.id == reference_id:
            return ref
    return None


@tool(name="list_references")
def list_references() -> dict:
    """List the files and items the user attached or referenced this turn.

    Returns each reference's kind, name, id, and transport so you can pick which
    one to fetch or query. Use the id with ``fetch_attachment`` (files) or the
    kind's dedicated tool (custom items).
    """
    references = get_references()
    if not references:
        return {"status": "success", "content": [{"text": "No references were provided."}]}
    lines = [
        f"- [{r.kind}] {r.name} (id={r.id}, transport={r.transport}"
        + (f", type={r.mime_type}" if r.mime_type else "")
        + ")"
        for r in references
    ]
    return {"status": "success", "content": [{"text": "\n".join(lines)}]}


@tool(name="fetch_attachment")
def fetch_attachment(reference_id: str) -> dict:
    """Fetch a file attachment's contents by its id.

    Text-like files (txt, md, csv, html, json, xml) are returned as text.
    Images, PDFs/office docs, and video are placed into the conversation as
    media the model can see directly — the tool confirms in text and the file
    appears in context on the next turn. Non-file references (custom entities)
    are not fetchable here — resolve those with the tool provided for their kind.

    Args:
        reference_id: The reference id from the manifest / ``list_references``.
    """
    ref = _find(reference_id)
    if ref is None:
        return {
            "status": "error",
            "content": [{"text": f"No reference with id {reference_id!r} was provided."}],
        }
    if ref.transport != "attachment":
        return {
            "status": "error",
            "content": [
                {
                    "text": (
                        f"Reference {ref.id!r} is a '{ref.kind}' item, not a file. "
                        "Resolve it with the tool provided for that kind."
                    )
                }
            ],
        }

    raw = _resolve_bytes(ref)
    if raw is None:
        return {
            "status": "error",
            "content": [{"text": f"Could not resolve the bytes for {ref.id!r}."}],
        }

    if _is_text_mime(ref.mime_type):
        return {"status": "success", "content": [{"text": raw.decode("utf-8", "replace")}]}

    # Media (image/document/video) can't be returned in a tool-role message on
    # OpenAI-compatible providers, so route it through the same user-message
    # injection the ``inline`` policy uses (works across providers). The tool
    # itself returns only text — the file lands in context before the next turn.
    if _content_block(ref, raw) is not None:
        request_inline(ref.id)
        return {
            "status": "success",
            "content": [
                {
                    "text": (
                        f"Loaded '{ref.name}' ({ref.mime_type or 'file'}). It is now "
                        "included in the conversation — read it directly to answer."
                    )
                }
            ],
        }

    return {
        "status": "error",
        "content": [
            {
                "text": (
                    f"Reference {ref.id!r} ({ref.mime_type or 'unknown type'}) is not a "
                    "supported inline media type and has no text representation."
                )
            }
        ],
    }
