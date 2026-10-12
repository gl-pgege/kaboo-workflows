# Call MCP tools as the signed-in user

When an agent's tools read a database or change a dashboard, they should do so
with the permissions of the person who asked — not a service account that can
see everything. The chain has three links: verify the caller when the run
arrives, bind them to the run, and relay their token on every MCP call.

```text
browser ──token──► host ──forwardedProps.runToken──► agent service
                                                       │ auth= verifier → Principal
                                                       ▼
                                   MCP client (auth: relay) ──token──► gateway ──► service
```

## Verify the caller when the run arrives

`create_agui_app(auth=...)` takes an async function that receives each
`/invocations` request and returns a `Principal` — or raises
`HTTPException(401)` to refuse it. Ours reads the token the host stamps into
`forwardedProps.runToken` and verifies it against the identity provider's
JWKS:

```{.python notest}
import os

import jwt
from fastapi import HTTPException, Request

from kaboo_workflows import Principal

JWKS = jwt.PyJWKClient(os.environ["OIDC_JWKS_URI"], cache_keys=True)


async def verify_run_token(request: Request) -> Principal | None:
    body = await request.json()
    token = (body.get("forwardedProps") or {}).get("runToken")
    if not token:
        raise HTTPException(status_code=401, detail="Missing forwardedProps.runToken")
    try:
        key = JWKS.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            issuer=os.environ.get("OIDC_ISSUER"),
            options={"verify_aud": False},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc
    return Principal(token=token, claims=claims)
```

The token travels in the body rather than a header because the host — a
CopilotKit runtime, a backend — is what calls the agent service, and it may
run work on the user's behalf with a token it obtained itself. Audience checks
belong at the services that finally receive the relayed token; which audience
a token carries depends on the gateway in between.

## Relay the token on every MCP call

`auth: relay` sends the bound principal's token with every request the MCP
client makes ([Chapter 9](../configuration/Chapter_09.md#outbound-auth-the-auth-field)).
Behind a gateway with one target per service, give each target its own client
with an exact tool allow-list, and anchor the auth block once:

```yaml
mcp_clients:
  dashboard:
    url: ${GATEWAY_URL}
    transport: streamable-http
    auth: &relay_auth
      type: relay
      params:
        header: [Authorization, x-run-token]
        context_headers:
          x-run-id: run_id
          x-timezone: forwarded_props.timezone
    params:
      tool_filters:
        allowed: [dashboard___get_dashboard, dashboard___update_widgets]
    tool_labels:
      dashboard___update_widgets: Updating the dashboard
    transport_options:
      terminate_on_close: false
      timeout: {read: 840}

  sql:
    url: ${GATEWAY_URL}
    transport: streamable-http
    auth: *relay_auth
    params:
      tool_filters:
        allowed: [sql___run_query, sql___get_schema]
```

- **Two headers** because a managed AgentCore Gateway authenticates the caller
  on `Authorization` and then replaces it with its own credential for the
  target. The second, allowlisted header is the one the service reads.
- **`context_headers`** tell the services which run a call belongs to, and
  pass along anything the client forwarded, such as the user's time zone.
- **`tool_filters.allowed`** names are exact matches, not globs. Narrow clients
  (a read-only `sql_explore` beside a full `sql`) let you grant an agent only
  what its job needs.
- **`terminate_on_close: false`** and a long read timeout suit gateways that
  hold long tool calls open.

## Only relay on clients built per run

A relayed token is per-request, so the client carrying it must be too. With
`session_config_key` set, `create_agui_app` builds a run's MCP clients for that
run and closes them when it ends — the token one run relays can never reach
another user's run. A process serving one fixed config shares its clients;
relay is only safe there when the process serves a single user, as an AgentCore
Runtime microVM does per session (`examples/agentcore/` shows both shapes).
`m2m` and `static` strategies carry no per-user identity and are safe on a
shared client anywhere.

## When there is no principal, or it expires

- A relay client with no principal in context omits the header, so the target
  answers 401. That is the first thing to check when a tool unexpectedly fails
  authorization.
- A long run can outlive the user's token. Stop it with a clear message instead
  of letting every tool fail —
  [`StopOnExpiredSignIn`](guard-hooks.md#stop-when-the-sign-in-expires).
- Background jobs that run with no user should use the service's own identity:
  have the host obtain a client-credentials token and send it the same way, so
  the agent service needs no second code path.
