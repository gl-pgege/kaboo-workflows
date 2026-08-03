"""Pluggable reference byte fetching, with optional authorization.

Every reference/attachment byte fetch in the library funnels through
:func:`fetch_reference_bytes`. By default it behaves exactly like the historic
private helper: presigned/public http(s) URLs only, no credentials, 30s
timeout, 25 MB cap.

Hosts whose files live behind an authenticated route have two options:

1. **Config** — set ``attachments.base_url`` / ``attachments.authorization``
   in the YAML config. The library builds a :class:`ConfiguredReferenceFetcher`
   that resolves relative URLs against the base and attaches a bearer token —
   strictly for URLs under that base, so credentials are never sent to
   third-party hosts (presigned/public URLs keep the default behavior).
2. **Code** — register any callable matching :class:`ReferenceFetcher` via
   :func:`set_reference_fetcher`. The callable receives the URL *and* the full
   :class:`~kaboo_workflows._context.Reference` (when available), so hosts with
   multiple authenticated stores or non-HTTP retrieval can route per reference.

The registered fetcher runs inside the request context, so it may read
per-run values (e.g. a run-scoped token) via
:func:`~kaboo_workflows._context.get_forwarded_props`.

:func:`install_agui_strands_fetch` routes ag-ui-strands' entry-message media
fetching through the same funnel, so first-turn inline attachments and
later-turn ``fetch_attachment`` calls behave identically.
"""

from __future__ import annotations

import logging
import os
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING, Protocol

from .._context import get_forwarded_props

if TYPE_CHECKING:
    from .._context import Reference

logger = logging.getLogger(__name__)

# Guard: server-side URL fetches inline bytes. Cap the size so a hostile/huge
# attachment can't exhaust memory.
_MAX_FETCH_BYTES = 25 * 1024 * 1024

_FORWARDED_PROPS_PREFIX = "forwarded_props:"
_ENV_PREFIX = "env:"
AUTHORIZATION_PREFIXES = (_FORWARDED_PROPS_PREFIX, _ENV_PREFIX)


class ReferenceFetcher(Protocol):
    """Strategy for resolving a reference URL to raw bytes.

    Implementations return ``None`` on any failure (the caller treats the
    reference as unresolvable). ``reference`` is provided when the fetch is on
    behalf of a parsed reference; URL-only call sites (entry-message media
    parts) pass ``None``.
    """

    def __call__(self, url: str, *, reference: Reference | None = None) -> bytes | None: ...


_fetcher: ReferenceFetcher | None = None


def set_reference_fetcher(fetcher: ReferenceFetcher | None) -> None:
    """Register the process-wide reference fetcher (``None`` restores default).

    Call once at startup, before the app serves requests. Per-run behavior
    belongs *inside* the fetcher (read the request context there), not in
    re-registration.
    """
    global _fetcher
    if fetcher is not None and not callable(fetcher):
        raise TypeError(
            f"reference fetcher must be callable or None, got {type(fetcher).__name__}."
        )
    _fetcher = fetcher


def get_reference_fetcher() -> ReferenceFetcher | None:
    """Return the registered reference fetcher (``None`` when default)."""
    return _fetcher


def fetch_reference_bytes(url: str, *, reference: Reference | None = None) -> bytes | None:
    """Fetch reference bytes via the registered fetcher (default when none)."""
    if _fetcher is not None:
        return _fetcher(url, reference=reference)
    return default_fetch(url)


def default_fetch(url: str, *, headers: dict[str, str] | None = None) -> bytes | None:
    """Fetch bytes from an http(s) URL, size-capped; no credentials by default.

    Rejects non-http(s) schemes (local-file read / SSRF guard) and anything
    over the 25 MB cap. Returns ``None`` on any failure.
    """
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        logger.warning("refusing to fetch reference url with non-http(s) scheme")
        return None
    request = urllib.request.Request(url, headers=headers or {})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:  # noqa: S310  # nosec B310
            data = resp.read(_MAX_FETCH_BYTES + 1)
    except Exception as exc:
        logger.warning("failed to fetch reference url: %s", exc)
        return None
    if len(data) > _MAX_FETCH_BYTES:
        logger.warning("reference exceeds %d-byte fetch cap; skipping", _MAX_FETCH_BYTES)
        return None
    return data


class ConfiguredReferenceFetcher:
    """Fetcher built from ``attachments.base_url`` / ``attachments.authorization``.

    - Relative URLs (``/attachments/…``) resolve against ``base_url``.
    - The authorization token is attached **only** to URLs under ``base_url``;
      any other URL is fetched with the unauthenticated default, so host
      credentials never leak to third-party origins.

    ``authorization`` spec formats:

    - ``forwarded_props:<key>`` — read the token from the current run's
      forwarded props (a run-scoped credential the host sends per invocation).
    - ``env:<VAR>`` — read a static token from the environment.
    """

    def __init__(self, *, base_url: str | None = None, authorization: str | None = None) -> None:
        self._base_url = base_url.rstrip("/") if base_url else None
        self._authorization = authorization

    def __call__(self, url: str, *, reference: Reference | None = None) -> bytes | None:
        if url.startswith("/") and self._base_url:
            url = self._base_url + url
        headers: dict[str, str] | None = None
        if self._is_own_origin(url):
            token = self._resolve_token()
            if token:
                headers = {"Authorization": f"Bearer {token}"}
        return default_fetch(url, headers=headers)

    def _is_own_origin(self, url: str) -> bool:
        if not self._base_url:
            return False
        return url == self._base_url or url.startswith(self._base_url + "/")

    def _resolve_token(self) -> str | None:
        spec = self._authorization
        if not spec:
            return None
        if spec.startswith(_FORWARDED_PROPS_PREFIX):
            value = get_forwarded_props().get(spec[len(_FORWARDED_PROPS_PREFIX) :])
            return value if isinstance(value, str) and value else None
        if spec.startswith(_ENV_PREFIX):
            return os.environ.get(spec[len(_ENV_PREFIX) :]) or None
        return None


# --- attachment URL synthesis (object-reference upgrade) ---------------------

_attachment_url_template: str | None = None


def set_attachment_url_template(template: str | None) -> None:
    """Register the ``attachments.content_url_template`` for reference parsing.

    When set, object references of kind ``"attachment"`` that arrive without a
    URL get one synthesized from the template (``{id}`` placeholder), which
    upgrades them to fetchable attachment transport on every turn.
    """
    global _attachment_url_template
    _attachment_url_template = template


def get_attachment_url_template() -> str | None:
    """Return the registered attachment content URL template (or ``None``)."""
    return _attachment_url_template


def install_agui_strands_fetch() -> None:
    """Route ag-ui-strands' media fetching through the kaboo fetcher.

    ag-ui-strands fetches entry-message media parts (``InputContentUrlSource``
    and media items) with a bare ``urlopen``. This rebinding sends those
    fetches through :func:`fetch_reference_bytes` so a registered fetcher (and
    the default's scheme guard + size cap) applies to first-turn inline media
    exactly as it does to ``fetch_attachment``.

    ag-ui-strands is a third-party package with no fetch extension point; this
    module-attribute rebinding is intentionally centralized here (pinned to the
    dependency version, covered by tests) instead of leaking into host apps.
    Both call sites resolve the module global by name at call time, so
    rebinding is effective regardless of import order.
    """
    from ag_ui_strands import utils as agui_utils

    def _fetch(url: str) -> bytes | None:
        return fetch_reference_bytes(url)

    agui_utils._fetch_url_bytes = _fetch  # ty: ignore[invalid-assignment]
