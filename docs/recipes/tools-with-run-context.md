# Tools that need run context

A tool is a plain function, but some need to know where they are running:
which conversation, which request, which user. The library binds that context
per run, and these functions read it from inside any tool or hook:

| Function | Returns | Use it for |
|----------|---------|------------|
| `get_thread_id()` | The conversation's id | State that should outlive one request, such as values an agent stores for later turns |
| `get_run_id()` | This request's id; a resume is a new request | Logs, metrics, per-request scratch space |
| `get_turn_id()` | The logical turn's id, the same across an interrupt and its resume | Keys for side effects that must happen once per turn |
| `get_forwarded_props()` | What the client forwarded with the run | Client settings such as the user's time zone |
| `get_auth_context()` | The run's `Principal`, or `None` | The caller's identity and token claims |

All of them return `None` (or an empty dict) outside a run, so a tool still
works when you call it directly in a unit test.

## Share values between agents in one run

Agents in a multi-agent run often need to hand each other something larger
than a delegation message: the findings one analyst produced, the table
another selected. A small run-scoped store does it, keyed by thread and run:

```python
import json
from collections import OrderedDict
from threading import Lock

from strands import tool

from kaboo_workflows import get_run_id, get_thread_id

_MAX_RUNS = 256
_stores: OrderedDict[str, dict[str, str]] = OrderedDict()
_lock = Lock()


def _store() -> dict[str, str]:
    key = f"{get_thread_id()}:{get_run_id()}"
    with _lock:
        store = _stores.setdefault(key, {})
        _stores.move_to_end(key)
        while len(_stores) > _MAX_RUNS:
            _stores.popitem(last=False)
        return store


@tool
def set_workflow_variable(name: str, value: str) -> dict:
    """Store a named value for other agents in this run to read.

    Args:
        name: The variable's name, such as "sql_findings".
        value: The value; serialize structured data as JSON.
    """
    _store()[name] = value
    return {"status": "ok", "name": name}


@tool
def get_workflow_variable(name: str) -> dict:
    """Read a value an earlier agent in this run stored.

    Args:
        name: The variable's name.
    """
    store = _store()
    if name not in store:
        return {"status": "not_found", "name": name, "available": sorted(store)}
    return {"status": "ok", "name": name, "value": store[name]}


set_workflow_variable("selected_table", json.dumps({"schema": "sales", "table": "orders"}))
assert get_workflow_variable("selected_table")["status"] == "ok"
```

Three details carry the weight:

- **The store is bounded.** One process serves many runs; an unbounded
  module-level dict is a slow memory leak.
- **A miss lists what is available.** The model recovers from a wrong name in
  one step instead of guessing.
- **Values are strings.** Asking the model for JSON keeps the tool schema
  simple and the contents inspectable.

A process-local store suits a single process per session, as on AgentCore
Runtime. Behind a load balancer, keep the same tool interface over Redis or a
database.

## Do a side effect once per turn

A run that pauses for approval and resumes is two requests, so the run id
changes between them. Key anything that must not happen twice on the turn id:

```python
from strands import tool

from kaboo_workflows import get_thread_id, get_turn_id

_sent: set[tuple[str | None, str | None, str]] = set()


@tool
def send_summary(recipient: str) -> str:
    """Email the conversation's summary to a recipient.

    Args:
        recipient: The email address to send to.
    """
    key = (get_thread_id(), get_turn_id(), recipient)
    if key in _sent:
        return f"Already sent to {recipient} this turn."
    _sent.add(key)
    return f"Sent to {recipient}."


assert send_summary("ops@example.com") == "Sent to ops@example.com."
assert send_summary("ops@example.com").startswith("Already sent")
```

## Read what the client forwarded, and who is asking

```python
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from strands import tool

from kaboo_workflows import get_auth_context, get_forwarded_props


@tool
def today() -> str:
    """Return today's date in the user's time zone."""
    zone = get_forwarded_props().get("timezone")
    try:
        tz = ZoneInfo(zone) if isinstance(zone, str) and zone else None
    except (ZoneInfoNotFoundError, ValueError):
        tz = None
    return datetime.now(tz).date().isoformat()


@tool
def whoami() -> str:
    """Return the signed-in user's name."""
    principal = get_auth_context()
    return principal.claims.get("name", "unknown") if principal else "anonymous"


assert whoami() == "anonymous"
```

Treat forwarded props as user input: they come from the client, so validate
them, as `today` does with the time zone. Identity comes only from
`get_auth_context()`, which the server's verifier set
([Call MCP tools as the signed-in user](user-identity-mcp.md)).

## Wire them in

Reference tools from the config by file or module:

```yaml
agents:
  analyst:
    tools:
      - ./tools/context_kv.py:set_workflow_variable
      - ./tools/context_kv.py:get_workflow_variable
```

Relative paths resolve from the config file's directory.
