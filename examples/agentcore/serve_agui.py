"""agentcore — serve kaboo-workflows with pluggable inbound auth (ergonomic path).

``create_agui_app(auth=...)`` adds an inbound verifier: it runs on every
``/invocations`` request, returns a ``Principal`` (or raises to reject), and
binds the identity to the request context so the declarative outbound ``auth:``
in ``config.yaml`` can relay / exchange from it.

Three inbound styles are shown; pick one with ``KABOO_AUTH_MODE``:

    relay  — verify the user JWT (signature/claims), then forward it to the MCP
             (``auth: relay`` in config.yaml).
    obo    — read the AgentCore WorkloadAccessToken (already validated by the
             Runtime authorizer) and carry it so the OBO strategy can exchange
             it (switch config.yaml to ``auth: obo``).
    m2m    — validate the caller's M2M token; the outbound call uses the
             service's own machine identity (switch config.yaml to ``auth: m2m``).

Run:
    KABOO_AUTH_MODE=relay uv run uvicorn examples.agentcore.serve_agui:app --port 8080

Note on deployment: ``create_agui_app`` starts one shared MCP client at boot and
strands snapshots the context when that client starts. ``relay`` / ``obo`` carry
*per-request* identity, so on a shared long-lived client they are reliable only
when the process serves one user/session (e.g. AgentCore Runtime: one microVM
per session). For a multi-tenant standalone process, use ``server_custom.py``
which creates an isolated per-request MCP client. ``m2m`` / ``static`` are
request-independent and safe on the shared client anywhere.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import HTTPException, Request

from kaboo_workflows import Principal
from kaboo_workflows.adapters import create_agui_app

CONFIG = Path(__file__).parent / "config.yaml"


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def verify_jwt_relay(request: Request) -> Principal:
    """Validate the user JWT, then relay it downstream (``auth: relay``)."""
    token = _bearer(request)
    if not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    # Replace with real validation (JWKS signature + aud/iss/exp checks), e.g.:
    #   claims = jwt.decode(token, jwks_key, audience=..., algorithms=["RS256"])
    claims = _decode_unverified(token)
    return Principal(token=token, claims=claims)


def read_workload_token(request: Request) -> Principal:
    """Read the AgentCore WorkloadAccessToken for an OBO exchange (``auth: obo``).

    On AgentCore Runtime the token is already validated by the inbound
    authorizer and injected as a header, so we only extract it here.
    """
    workload = request.headers.get("WorkloadAccessToken") or _bearer(request)
    if not workload:
        raise HTTPException(status_code=401, detail="missing workload token")
    return Principal(token=workload, headers={"WorkloadAccessToken": workload})


def verify_m2m(request: Request) -> Principal:
    """Validate the caller's machine token; outbound uses the service identity."""
    token = _bearer(request)
    if not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    # Validate the M2M token (introspection or signature). We do NOT forward it;
    # the outbound MCP call mints the service's own token via ``auth: m2m``.
    claims = _decode_unverified(token)
    return Principal(token=None, claims=claims)


def _decode_unverified(token: str) -> dict[str, object]:
    """Best-effort claim decode for the demo (DO NOT use in production)."""
    import base64
    import binascii
    import json

    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, binascii.Error):
        return {}


_VERIFIERS = {
    "relay": verify_jwt_relay,
    "obo": read_workload_token,
    "m2m": verify_m2m,
}

_mode = os.environ.get("KABOO_AUTH_MODE", "relay")
app = create_agui_app(CONFIG, auth=_VERIFIERS[_mode])
