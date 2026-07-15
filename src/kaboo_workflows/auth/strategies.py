"""Outbound MCP authentication strategies.

Each strategy is an :class:`httpx.Auth` flow that stamps a downstream bearer
credential onto every MCP HTTP request. Wire one into an MCP client via
``transport_options`` (``http_client`` for streamable-http, ``auth`` for SSE) or
declaratively via the ``auth:`` field on an ``mcp_clients:`` entry.

Two identity sources are supported per strategy:

- **Per-request identity** — the inbound :class:`~kaboo_workflows._context.Principal`
  bound by the AG-UI server (``relay`` / ``obo``). Because strands snapshots the
  context when an MCP client *starts* (``contextvars.copy_context()``), this is
  reliable only when the client is created/started inside the request's context
  (a per-request client), not for a long-lived shared client started at boot.
- **Explicit / machine identity** — a token passed at construction, or
  client-credentials (``static`` / ``m2m``), which is request-independent and
  therefore safe on a shared client.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING, Any

import httpx

from .._context import get_auth_context

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator

_LEEWAY_SECONDS = 60.0
"""Refresh a cached token this many seconds before its stated expiry."""


class _BearerAuth(httpx.Auth):
    """Apply a bearer token (resolved per request) to the outgoing request.

    Subclasses implement :meth:`_token`. Strategies that mint tokens with
    blocking I/O override :meth:`_token_async` to offload to a worker thread so
    the event loop is never blocked.
    """

    header_name: str = "Authorization"
    scheme: str = "Bearer"

    def _token(self) -> str | None:  # pragma: no cover - overridden
        raise NotImplementedError

    async def _token_async(self) -> str | None:
        return self._token()

    def _apply(self, request: httpx.Request, token: str | None) -> None:
        if not token:
            return
        request.headers[self.header_name] = f"{self.scheme} {token}" if self.scheme else token

    def sync_auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        self._apply(request, self._token())
        yield request

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        self._apply(request, await self._token_async())
        yield request


class RelayTokenAuth(_BearerAuth):
    """Forward the inbound caller token to the MCP unchanged.

    Reads the current :class:`~kaboo_workflows._context.Principal` token unless
    an explicit ``token`` is given (useful when binding a per-request client
    with a captured token).

    Args:
        token: Explicit token to forward. When ``None``, the inbound
            principal's token is used at call time.
        header: Header to set (default ``Authorization``).
        scheme: Auth scheme prefix (default ``Bearer``; ``""`` for a raw value).
    """

    def __init__(
        self, *, token: str | None = None, header: str = "Authorization", scheme: str = "Bearer"
    ) -> None:
        self._explicit = token
        self.header_name = header
        self.scheme = scheme

    def _token(self) -> str | None:
        if self._explicit is not None:
            return self._explicit
        principal = get_auth_context()
        return principal.token if principal else None


class StaticTokenAuth(_BearerAuth):
    """Attach a fixed token (e.g. a long-lived API key) to every MCP request.

    Args:
        token: The credential to send.
        header: Header to set (default ``Authorization``).
        scheme: Auth scheme prefix (default ``Bearer``; ``""`` for a raw value).
    """

    def __init__(
        self, *, token: str, header: str = "Authorization", scheme: str = "Bearer"
    ) -> None:
        self._value = token
        self.header_name = header
        self.scheme = scheme

    def _token(self) -> str | None:
        return self._value


class OBOTokenAuth(_BearerAuth):
    """AgentCore On-Behalf-Of token exchange for a downstream resource.

    Exchanges the caller's AgentCore ``WorkloadAccessToken`` for a scoped
    downstream access token via
    ``bedrock-agentcore:GetResourceOauth2Token`` and caches it per workload
    token until shortly before expiry.

    The workload token is read from the inbound principal (its ``token`` or its
    ``WorkloadAccessToken`` header) unless an explicit ``workload_token`` is
    supplied.

    Args:
        provider: Name of the AgentCore OAuth2 credential provider (the
            downstream resource) to exchange for.
        region: AWS region of the AgentCore control plane.
        scopes: Optional OAuth2 scopes to request.
        workload_token: Explicit workload token (bypasses the principal).
        header: Header to set (default ``Authorization``).
        scheme: Auth scheme prefix (default ``Bearer``).
    """

    def __init__(
        self,
        *,
        provider: str,
        region: str = "us-east-1",
        scopes: list[str] | None = None,
        workload_token: str | None = None,
        header: str = "Authorization",
        scheme: str = "Bearer",
    ) -> None:
        self._provider = provider
        self._region = region
        self._scopes = scopes
        self._explicit_workload = workload_token
        self.header_name = header
        self.scheme = scheme
        self._client: Any = None
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def _boto(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("bedrock-agentcore", region_name=self._region)
        return self._client

    def _workload_token(self) -> str | None:
        if self._explicit_workload is not None:
            return self._explicit_workload
        principal = get_auth_context()
        if principal is None:
            return None
        return principal.token or principal.headers.get("WorkloadAccessToken")

    def _token(self) -> str | None:
        workload = self._workload_token()
        if not workload:
            return None
        now = time.time()
        cached = self._cache.get(workload)
        if cached and cached[1] > now + _LEEWAY_SECONDS:
            return cached[0]
        with self._lock:
            cached = self._cache.get(workload)
            if cached and cached[1] > now + _LEEWAY_SECONDS:
                return cached[0]
            kwargs: dict[str, Any] = {
                "workloadIdentityToken": workload,
                "resourceCredentialProviderName": self._provider,
                "oauth2Flow": "ON_BEHALF_OF_TOKEN_EXCHANGE",
            }
            if self._scopes:
                kwargs["scopes"] = self._scopes
            resp = self._boto().get_resource_oauth2_token(**kwargs)
            token: str = resp["accessToken"]
            expires_in = float(resp.get("expiresIn", 3600))
            self._cache[workload] = (token, now + expires_in)
            return token

    async def _token_async(self) -> str | None:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._token)


class M2MClientCredentialsAuth(_BearerAuth):
    """OAuth2 client-credentials (machine-to-machine) token for MCP calls.

    Fetches a token from ``token_url`` using the client-credentials grant and
    caches it until shortly before expiry. Independent of the inbound caller —
    represents the workflow service's own machine identity, so it is safe on a
    long-lived shared MCP client.

    Args:
        token_url: OAuth2 token endpoint.
        client_id: Client identifier.
        client_secret: Client secret.
        scope: Optional space-delimited scopes.
        audience: Optional audience parameter (e.g. Auth0).
        extra: Extra form fields to include in the token request.
        header: Header to set (default ``Authorization``).
        scheme: Auth scheme prefix (default ``Bearer``).
    """

    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        client_secret: str,
        scope: str | None = None,
        audience: str | None = None,
        extra: dict[str, str] | None = None,
        header: str = "Authorization",
        scheme: str = "Bearer",
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._audience = audience
        self._extra = extra or {}
        self.header_name = header
        self.scheme = scheme
        self._cache: tuple[str, float] | None = None
        self._lock = threading.Lock()

    def _fetch(self) -> tuple[str, float]:
        data: dict[str, str] = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        if self._scope:
            data["scope"] = self._scope
        if self._audience:
            data["audience"] = self._audience
        data.update(self._extra)
        resp = httpx.post(self._token_url, data=data)
        resp.raise_for_status()
        payload = resp.json()
        return payload["access_token"], time.time() + float(payload.get("expires_in", 3600))

    def _token(self) -> str | None:
        now = time.time()
        cached = self._cache
        if cached and cached[1] > now + _LEEWAY_SECONDS:
            return cached[0]
        with self._lock:
            cached = self._cache
            if cached and cached[1] > now + _LEEWAY_SECONDS:
                return cached[0]
            token, expiry = self._fetch()
            self._cache = (token, expiry)
            return token

    async def _token_async(self) -> str | None:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._token)


_STRATEGIES: dict[str, type[_BearerAuth]] = {
    "relay": RelayTokenAuth,
    "static": StaticTokenAuth,
    "obo": OBOTokenAuth,
    "m2m": M2MClientCredentialsAuth,
}


def build_auth(strategy: str, params: dict[str, Any] | None = None) -> httpx.Auth:
    """Build an outbound MCP auth strategy from a name + params.

    Args:
        strategy: One of ``relay``, ``static``, ``obo``, ``m2m``.
        params: Constructor keyword arguments for the strategy.

    Returns:
        A configured :class:`httpx.Auth` instance.

    Raises:
        ValueError: If ``strategy`` is not a known strategy name.
    """
    try:
        cls = _STRATEGIES[strategy]
    except KeyError:
        raise ValueError(
            f"Unknown MCP auth strategy '{strategy}'. Available: {', '.join(sorted(_STRATEGIES))}."
        ) from None
    return cls(**(params or {}))


def apply_auth_to_transport_options(
    options: dict[str, Any] | None,
    auth: httpx.Auth,
    *,
    transport: str,
) -> dict[str, Any]:
    """Return ``transport_options`` with *auth* wired in for the given transport.

    SSE accepts an ``auth`` kwarg directly. Streamable-http has no ``auth``
    kwarg, so the auth rides a dedicated ``httpx.AsyncClient`` (folding in any
    ``headers`` the caller set). A user-provided ``http_client`` / ``auth`` is
    left untouched.
    """
    opts = dict(options or {})
    if transport == "sse":
        opts.setdefault("auth", auth)
        return opts
    if "http_client" not in opts:
        headers = opts.pop("headers", None)
        opts["http_client"] = httpx.AsyncClient(auth=auth, headers=headers or None)
    return opts
