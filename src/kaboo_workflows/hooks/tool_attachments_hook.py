"""Media a tool returns as data, unpacked into blocks the model can see.

A tool behind an MCP gateway can only answer in text: the gateway reduces every
HTTP response to JSON or text, and binary responses do not pass at all. A tool
that wants the model to *see* an image returns it inside its JSON as::

    {"attachments": [{"name": "Rows 1-7", "mime_type": "image/jpeg", "data": "<base64>"}]}

:class:`ToolAttachmentsHook` removes that field before the model reads the
result and appends each supported attachment as a media block of the same tool
result, after a text block carrying its name. The bytes never reach the model as
text, and strands carries media in a tool result to every provider (the OpenAI
formatter moves it into a following user message itself).
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import sys
from typing import TYPE_CHECKING, Any, cast

from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import AfterToolCallEvent

from ..tools.references import media_block

if TYPE_CHECKING:
    from strands.types.tools import ToolResult

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

logger = logging.getLogger(__name__)

ATTACHMENTS_FIELD = "attachments"
_MAX_ATTACHMENTS = 8
_MAX_ATTACHMENT_BYTES = 3_750_000


def _attachment_blocks(item: Any) -> list[dict[str, Any]]:
    """Text naming one attachment plus its media block, or a note why it was dropped."""
    if not isinstance(item, dict) or not isinstance(item.get("data"), str):
        return []
    name = str(item.get("name") or "attachment")
    try:
        raw = base64.b64decode(item["data"], validate=True)
    except (binascii.Error, ValueError):
        return [{"text": f"{name}: dropped, its data is not valid base64."}]
    if len(raw) > _MAX_ATTACHMENT_BYTES:
        return [
            {
                "text": f"{name}: dropped, {len(raw)} bytes is over the {_MAX_ATTACHMENT_BYTES}-byte limit."
            }
        ]
    block = media_block(item.get("mime_type"), name, raw)
    if block is None:
        return [
            {
                "text": f"{name}: dropped, {item.get('mime_type') or 'no type'} is not a supported media type."
            }
        ]
    return [{"text": name}, block]


def unpack_attachments(content: list[Any]) -> list[dict[str, Any]] | None:
    """Return ``content`` with every ``attachments`` field unpacked, or ``None`` if there was none.

    Looks in ``text`` blocks whose text is a JSON object and in ``json`` blocks.
    At most ``_MAX_ATTACHMENTS`` attachments are unpacked per result.
    """
    unpacked: list[dict[str, Any]] = []
    media: list[dict[str, Any]] = []
    found = False
    for block in content:
        body: Any = block.get("json") if "json" in block else None
        if body is None and isinstance(block.get("text"), str):
            try:
                body = json.loads(block["text"])
            except ValueError:
                body = None
        if not isinstance(body, dict) or not isinstance(body.get(ATTACHMENTS_FIELD), list):
            unpacked.append(block)
            continue
        found = True
        attachments = body.pop(ATTACHMENTS_FIELD)
        unpacked.append(
            {"json": body} if "json" in block else {"text": json.dumps(body, ensure_ascii=False)}
        )
        for item in attachments[:_MAX_ATTACHMENTS]:
            media.extend(_attachment_blocks(item))
        if len(attachments) > _MAX_ATTACHMENTS:
            media.append(
                {"text": f"{len(attachments) - _MAX_ATTACHMENTS} more attachment(s) dropped."}
            )
    return [*unpacked, *media] if found else None


class ToolAttachmentsHook(HookProvider):
    """Unpack ``attachments`` in a tool result into media blocks of that result.

    Registered on every agent the config builds; a result without the field is
    left untouched. An MCP result's ``structuredContent`` copy loses the field
    too, so the base64 is not kept beside the media.
    """

    @override
    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        """Register the after-tool-call callback."""
        registry.add_callback(AfterToolCallEvent, self._unpack)

    def _unpack(self, event: AfterToolCallEvent) -> None:
        result = event.result
        content = result.get("content") if isinstance(result, dict) else None
        if not isinstance(content, list):
            return
        unpacked = unpack_attachments(content)
        if unpacked is None:
            return
        replaced: dict[str, Any] = {**result, "content": unpacked}
        structured = replaced.get("structuredContent")
        if isinstance(structured, dict) and ATTACHMENTS_FIELD in structured:
            replaced["structuredContent"] = {
                k: v for k, v in structured.items() if k != ATTACHMENTS_FIELD
            }
        event.result = cast("ToolResult", replaced)
        logger.debug(
            "tool=<%s> | unpacked %d media block(s) from a tool result",
            event.tool_use.get("name"),
            sum(1 for b in unpacked if "image" in b or "document" in b or "video" in b),
        )
