"""Pluggable reference fetching.

``fetch_reference_bytes`` routes through a registered ``ReferenceFetcher``
(default behavior when none), and ``ConfiguredReferenceFetcher`` resolves
relative URLs against ``base_url`` and attaches the bearer token strictly to
own-origin URLs. No network is touched: the underlying ``default_fetch`` is
monkeypatched to capture calls.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from kaboo_workflows._context import Reference, set_forwarded_props
from kaboo_workflows.tools import fetching
from kaboo_workflows.tools.fetching import (
    ConfiguredReferenceFetcher,
    default_fetch,
    fetch_reference_bytes,
    get_reference_fetcher,
    install_agui_strands_fetch,
    set_reference_fetcher,
)


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    set_reference_fetcher(None)
    set_forwarded_props(None)
    yield
    set_reference_fetcher(None)
    set_forwarded_props(None)


def test_default_fetch_rejects_non_http_schemes():
    assert default_fetch("file:///etc/passwd") is None
    assert default_fetch("ftp://host/x") is None


def test_registry_roundtrip_and_reset():
    def fetcher(url: str, *, reference: Reference | None = None) -> bytes | None:
        return b"x"

    set_reference_fetcher(fetcher)
    assert get_reference_fetcher() is fetcher
    set_reference_fetcher(None)
    assert get_reference_fetcher() is None


def test_registry_rejects_non_callable():
    with pytest.raises(TypeError, match="callable"):
        set_reference_fetcher("not-a-fetcher")  # ty: ignore[invalid-argument-type]


def test_fetch_reference_bytes_routes_through_registered_fetcher():
    seen: list[tuple[str, str | None]] = []

    def fetcher(url: str, *, reference: Reference | None = None) -> bytes | None:
        seen.append((url, reference.id if reference else None))
        return b"bytes"

    set_reference_fetcher(fetcher)
    ref = Reference(kind="attachment", id="r1", name="a.pdf")
    assert fetch_reference_bytes("https://x/y", reference=ref) == b"bytes"
    assert seen == [("https://x/y", "r1")]


def _capture_default(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict | None]]:
    calls: list[tuple[str, dict | None]] = []

    def fake(url: str, *, headers: dict | None = None) -> bytes | None:
        calls.append((url, headers))
        return b"ok"

    monkeypatch.setattr(fetching, "default_fetch", fake)
    return calls


def test_configured_fetcher_resolves_relative_urls(monkeypatch: pytest.MonkeyPatch):
    calls = _capture_default(monkeypatch)
    fetcher = ConfiguredReferenceFetcher(base_url="https://api.example.com/")
    fetcher("/attachments/a1/content")
    assert calls[0][0] == "https://api.example.com/attachments/a1/content"


def test_configured_fetcher_token_only_for_own_origin(monkeypatch: pytest.MonkeyPatch):
    calls = _capture_default(monkeypatch)
    set_forwarded_props({"runToken": "tok-123"})
    fetcher = ConfiguredReferenceFetcher(
        base_url="https://api.example.com", authorization="forwarded_props:runToken"
    )
    fetcher("/attachments/a1/content")
    fetcher("https://s3.amazonaws.com/bucket/x.pdf?sig=1")
    own, other = calls
    assert own[1] == {"Authorization": "Bearer tok-123"}
    assert other[1] is None


def test_configured_fetcher_missing_token_fetches_unauthenticated(
    monkeypatch: pytest.MonkeyPatch,
):
    calls = _capture_default(monkeypatch)
    fetcher = ConfiguredReferenceFetcher(
        base_url="https://api.example.com", authorization="forwarded_props:runToken"
    )
    fetcher("/attachments/a1/content")
    assert calls[0][1] is None


def test_configured_fetcher_env_token(
    monkeypatch: pytest.MonkeyPatch,
):
    calls = _capture_default(monkeypatch)
    monkeypatch.setenv("KABOO_TEST_TOKEN", "env-tok")
    fetcher = ConfiguredReferenceFetcher(
        base_url="https://api.example.com", authorization="env:KABOO_TEST_TOKEN"
    )
    fetcher("https://api.example.com/attachments/a1/content")
    assert calls[0][1] == {"Authorization": "Bearer env-tok"}


def test_own_origin_requires_path_boundary(monkeypatch: pytest.MonkeyPatch):
    """A hostile lookalike origin must not receive the token."""
    calls = _capture_default(monkeypatch)
    set_forwarded_props({"runToken": "tok"})
    fetcher = ConfiguredReferenceFetcher(
        base_url="https://api.example.com", authorization="forwarded_props:runToken"
    )
    fetcher("https://api.example.com.evil.io/attachments/a1/content")
    assert calls[0][1] is None


def test_install_agui_strands_fetch_routes_through_registered_fetcher():
    from ag_ui_strands import utils as agui_utils

    original = agui_utils._fetch_url_bytes
    try:
        install_agui_strands_fetch()

        def fetcher(url: str, *, reference: Reference | None = None) -> bytes | None:
            return b"routed:" + url.encode()

        set_reference_fetcher(fetcher)
        assert agui_utils._fetch_url_bytes("https://x/y") == b"routed:https://x/y"
    finally:
        agui_utils._fetch_url_bytes = original
