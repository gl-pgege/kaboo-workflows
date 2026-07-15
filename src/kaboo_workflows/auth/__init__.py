"""Authentication helpers for kaboo-workflows.

Inbound identity is carried by :class:`~kaboo_workflows._context.Principal`
(read via :func:`~kaboo_workflows._context.get_auth_context`). Outbound MCP auth
is expressed with the :class:`httpx.Auth` strategies below.
"""

from __future__ import annotations

from .strategies import (
    M2MClientCredentialsAuth,
    OBOTokenAuth,
    RelayTokenAuth,
    StaticTokenAuth,
    apply_auth_to_transport_options,
    build_auth,
)

__all__ = [
    "M2MClientCredentialsAuth",
    "OBOTokenAuth",
    "RelayTokenAuth",
    "StaticTokenAuth",
    "apply_auth_to_transport_options",
    "build_auth",
]
