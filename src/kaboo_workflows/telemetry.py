"""Opt-in OpenTelemetry tracing for kaboo workflows.

Everything here speaks the OTel *API* only until telemetry is explicitly
enabled: with ``telemetry.enabled: false`` (the default) no SDK is configured,
every instrumentation call is a no-op, and runs behave byte-identically to a
build without this module.

When enabled (config ``telemetry.enabled: true``, overridable in either
direction by the ``KABOO_TELEMETRY_ENABLED`` env var), the process initializes
strands' :class:`~strands.telemetry.StrandsTelemetry` — which emits agent /
cycle / model / tool spans with GenAI semantic conventions — and exports them
via OTLP/HTTP to any backend (Langfuse, Phoenix, a bare collector, …).

Two kaboo-specific pieces ride on top:

- :class:`KabooContextSpanProcessor` stamps every span with the current
  conversation context (``session.id`` = thread id, ``user.id`` from the
  authenticated principal, ``kaboo.run.id`` / ``kaboo.turn.id``) plus static
  ``trace_attributes`` from config, so any backend can group spans by
  conversation and user without kaboo-specific code.
- :func:`current_trace_id` exposes the active trace id so the event stream
  (``AGENT_COMPLETE`` → ``ACTIVITY_SNAPSHOT``) can carry the correlation id
  out to the host UI, where feedback scores attach to the exact trace.

Spans stream through OTel batch processors — nothing buffers whole runs.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import SpanProcessor

if TYPE_CHECKING:
    from opentelemetry.context import Context
    from opentelemetry.sdk.trace import Span

    from .config.schema import TelemetryDef

logger = logging.getLogger(__name__)

_ENV_ENABLED = "KABOO_TELEMETRY_ENABLED"
_ENV_ENDPOINTS = ("KABOO_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT")
_ENV_HEADERS = ("KABOO_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_HEADERS")

_initialized = False
_enabled = False


def telemetry_enabled() -> bool:
    """Return whether telemetry was enabled by :func:`init_telemetry`."""
    return _enabled


def _env_flag(value: str | None) -> bool | None:
    """Parse a boolean env override; ``None`` when unset/unrecognized."""
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    return None


def parse_headers(value: dict[str, str] | str | None) -> dict[str, str] | None:
    """Normalize OTLP headers config to a dict.

    Accepts a mapping (returned as-is) or the ``OTEL_EXPORTER_OTLP_HEADERS``
    wire format (``key=value,key2=value2`` — values may contain ``=``).
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value) or None
    headers: dict[str, str] = {}
    for segment in value.split(","):
        segment = segment.strip()
        if not segment or "=" not in segment:
            continue
        key, _, val = segment.partition("=")
        headers[key.strip()] = val.strip()
    return headers or None


def resolve_traces_endpoint(endpoint: str | None) -> str | None:
    """Resolve the OTLP traces URL from config or env, appending ``/v1/traces``.

    The OTLP/HTTP exporter uses an explicit ``endpoint=`` kwarg verbatim
    (unlike the env var, where the SDK appends the signal path), so the
    ``/v1/traces`` suffix is added here when missing.
    """
    resolved = endpoint
    if not resolved:
        for env_name in _ENV_ENDPOINTS:
            value = os.getenv(env_name)
            if value:
                resolved = value
                break
    if not resolved:
        return None
    resolved = resolved.rstrip("/")
    if not resolved.endswith("/v1/traces"):
        resolved = f"{resolved}/v1/traces"
    return resolved


def _resolve_headers(configured: dict[str, str] | str | None) -> dict[str, str] | None:
    """Resolve OTLP headers from config or env fallbacks."""
    headers = parse_headers(configured)
    if headers:
        return headers
    for env_name in _ENV_HEADERS:
        headers = parse_headers(os.getenv(env_name))
        if headers:
            return headers
    return None


class KabooContextSpanProcessor(SpanProcessor):
    """SpanProcessor that stamps kaboo conversation context onto every span.

    Reads the request-scoped contextvars (bound by the AG-UI endpoint before
    any run task starts) at span start, so spans created anywhere inside a
    run — agent, model, tool — carry the conversation they belong to:

    - ``session.id`` — the AG-UI thread id (Langfuse groups traces by it)
    - ``user.id`` — the authenticated caller (``sub`` claim when present)
    - ``kaboo.run.id`` / ``kaboo.turn.id`` — kaboo's own correlation ids

    Static ``trace_attributes`` from config are stamped on every span too.
    """

    def __init__(self, static_attributes: dict[str, Any] | None = None) -> None:
        """Initialize with optional static attributes stamped on every span."""
        self._static = dict(static_attributes or {})

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        """Stamp conversation context and static attributes onto *span*."""
        from ._context import get_auth_context, get_run_id, get_thread_id, get_turn_id

        for key, value in self._static.items():
            span.set_attribute(key, value)
        thread_id = get_thread_id()
        if thread_id:
            span.set_attribute("session.id", thread_id)
        run_id = get_run_id()
        if run_id:
            span.set_attribute("kaboo.run.id", run_id)
        turn_id = get_turn_id()
        if turn_id:
            span.set_attribute("kaboo.turn.id", turn_id)
        principal = get_auth_context()
        if principal is not None:
            user_id = principal.claims.get("sub") or principal.claims.get("username")
            if user_id:
                span.set_attribute("user.id", str(user_id))


def init_telemetry(config: TelemetryDef | None) -> bool:
    """Initialize process-wide tracing from a config's ``telemetry`` section.

    Idempotent — the first call wins; later calls (e.g. per-run session
    configs) return the already-decided state without touching the tracer
    provider. When disabled this configures nothing: no SDK provider, no
    exporters, no overhead.

    Resolution order for ``enabled``: the ``KABOO_TELEMETRY_ENABLED`` env var
    (either direction) overrides the config flag.

    Args:
        config: The ``telemetry:`` section of an :class:`AppConfig`
            (``None`` behaves as the all-defaults section).

    Returns:
        Whether telemetry is enabled for this process.

    Raises:
        ImportError: Telemetry is enabled but the OTLP exporter is not
            installed (``pip install 'kaboo-workflows[telemetry]'``).
    """
    global _initialized, _enabled
    if _initialized:
        return _enabled
    _initialized = True

    from .config.schema import TelemetryDef

    cfg = config or TelemetryDef()
    override = _env_flag(os.getenv(_ENV_ENABLED))
    enabled = cfg.enabled if override is None else override
    if not enabled:
        _enabled = False
        return False

    try:
        from opentelemetry.exporter.otlp.proto.http import (  # noqa: F401
            trace_exporter,
        )
    except ImportError as exc:
        raise ImportError(
            "telemetry.enabled is true but the OTLP exporter is not installed. "
            "Install it with: pip install 'kaboo-workflows[telemetry]'"
        ) from exc

    from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider
    from strands.telemetry import StrandsTelemetry
    from strands.telemetry.config import get_otel_resource

    os.environ.setdefault("OTEL_SERVICE_NAME", cfg.service_name)

    # Respect an existing global SDK TracerProvider (a host app may have
    # configured OTel already); otherwise create one — with sampling when
    # requested — and set it as the global provider.
    current = trace_api.get_tracer_provider()
    if isinstance(current, SDKTracerProvider):
        provider = current
        telemetry = StrandsTelemetry(tracer_provider=provider)
    elif cfg.sample_ratio < 1.0:
        from opentelemetry.sdk.trace.sampling import ParentBasedTraceIdRatio

        provider = SDKTracerProvider(
            resource=get_otel_resource(),
            sampler=ParentBasedTraceIdRatio(cfg.sample_ratio),
        )
        trace_api.set_tracer_provider(provider)
        telemetry = StrandsTelemetry(tracer_provider=provider)
    else:
        telemetry = StrandsTelemetry()
        provider = telemetry.tracer_provider

    provider.add_span_processor(KabooContextSpanProcessor(cfg.trace_attributes))

    exporter_kwargs: dict[str, Any] = {}
    endpoint = resolve_traces_endpoint(cfg.otlp.endpoint)
    if endpoint:
        exporter_kwargs["endpoint"] = endpoint
    headers = _resolve_headers(cfg.otlp.headers)
    if headers:
        exporter_kwargs["headers"] = headers
    telemetry.setup_otlp_exporter(**exporter_kwargs)

    if cfg.console:
        telemetry.setup_console_exporter()

    logger.info(
        "telemetry enabled service=<%s> endpoint=<%s> sample_ratio=<%s>",
        cfg.service_name,
        endpoint or "(env-default)",
        cfg.sample_ratio,
    )
    _enabled = True
    return True


def current_trace_id(*, span: Any | None = None) -> str | None:
    """Return the active trace id as 32-char hex, or ``None``.

    Reads the ambient current span (strands activates the agent span around
    the run loop) unless an explicit *span* is given. Returns ``None`` when
    telemetry is disabled or no valid span is recording, so callers can
    unconditionally call this with zero overhead in the disabled case.
    """
    if not _enabled:
        return None
    target = span if span is not None else trace_api.get_current_span()
    try:
        ctx = target.get_span_context()
    except AttributeError:
        return None
    if ctx is None or not ctx.trace_id:
        return None
    return format(ctx.trace_id, "032x")


def _reset_for_tests() -> None:
    """Reset module state (test isolation only)."""
    global _initialized, _enabled
    _initialized = False
    _enabled = False
