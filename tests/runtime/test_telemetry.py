"""Tests for the opt-in telemetry module."""

from __future__ import annotations

import pytest
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider

from kaboo_workflows import telemetry
from kaboo_workflows._context import Principal, set_activity_context, set_auth_context
from kaboo_workflows.config.schema import TelemetryDef
from kaboo_workflows.telemetry import (
    KabooContextSpanProcessor,
    current_trace_id,
    init_telemetry,
    parse_headers,
    resolve_traces_endpoint,
    telemetry_enabled,
)


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    telemetry._reset_for_tests()
    monkeypatch.delenv("KABOO_TELEMETRY_ENABLED", raising=False)
    monkeypatch.delenv("KABOO_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("KABOO_OTLP_HEADERS", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)
    yield
    telemetry._reset_for_tests()


# --- disabled path ----------------------------------------------------------


def test_disabled_by_default_is_noop():
    assert init_telemetry(TelemetryDef()) is False
    assert telemetry_enabled() is False
    assert current_trace_id() is None


def test_none_config_behaves_as_defaults():
    assert init_telemetry(None) is False


def test_kill_switch_overrides_enabled(monkeypatch):
    monkeypatch.setenv("KABOO_TELEMETRY_ENABLED", "false")
    assert init_telemetry(TelemetryDef(enabled=True)) is False


def test_env_can_enable_without_config(monkeypatch):
    monkeypatch.setenv("KABOO_TELEMETRY_ENABLED", "true")
    monkeypatch.setenv("KABOO_OTLP_ENDPOINT", "http://localhost:9/otel")
    assert init_telemetry(TelemetryDef()) is True
    assert telemetry_enabled() is True


def test_init_is_idempotent_first_call_wins():
    assert init_telemetry(TelemetryDef()) is False
    assert init_telemetry(TelemetryDef(enabled=True)) is False


# --- config resolution helpers ----------------------------------------------


def test_parse_headers_from_wire_format():
    parsed = parse_headers("Authorization=Basic abc=,x-version=4")
    assert parsed == {"Authorization": "Basic abc=", "x-version": "4"}


def test_parse_headers_dict_passthrough():
    assert parse_headers({"a": "b"}) == {"a": "b"}
    assert parse_headers(None) is None
    assert parse_headers("") is None


def test_resolve_traces_endpoint_appends_signal_path():
    assert (
        resolve_traces_endpoint("http://host:3010/api/public/otel")
        == "http://host:3010/api/public/otel/v1/traces"
    )
    assert resolve_traces_endpoint("http://host:4318/v1/traces") == "http://host:4318/v1/traces"


def test_resolve_traces_endpoint_env_fallback(monkeypatch):
    monkeypatch.setenv("KABOO_OTLP_ENDPOINT", "http://env-host/otel")
    assert resolve_traces_endpoint(None) == "http://env-host/otel/v1/traces"


def test_resolve_traces_endpoint_none_when_unset():
    assert resolve_traces_endpoint(None) is None


# --- context span processor ---------------------------------------------------


def _provider_with_processor(**kwargs) -> SDKTracerProvider:
    provider = SDKTracerProvider()
    provider.add_span_processor(KabooContextSpanProcessor(**kwargs))
    return provider


def test_context_processor_stamps_conversation_attributes():
    set_activity_context("thread-1", "run-1", "turn-1")
    set_auth_context(Principal(claims={"sub": "user-42"}))
    try:
        provider = _provider_with_processor(static_attributes={"deployment": "test"})
        tracer = provider.get_tracer("test")
        span = tracer.start_span("probe")
        attrs = dict(span.attributes or {})
        span.end()
        assert attrs["session.id"] == "thread-1"
        assert attrs["kaboo.run.id"] == "run-1"
        assert attrs["kaboo.turn.id"] == "turn-1"
        assert attrs["user.id"] == "user-42"
        assert attrs["deployment"] == "test"
    finally:
        set_activity_context(None, None, None)
        set_auth_context(None)


def test_context_processor_omits_unbound_context():
    set_activity_context(None, None, None)
    set_auth_context(None)
    provider = _provider_with_processor()
    tracer = provider.get_tracer("test")
    span = tracer.start_span("probe")
    attrs = dict(span.attributes or {})
    span.end()
    assert "session.id" not in attrs
    assert "user.id" not in attrs


# --- current_trace_id ----------------------------------------------------------


def test_current_trace_id_from_active_span():
    telemetry._enabled = True
    provider = SDKTracerProvider()
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("probe") as span:
        expected = format(span.get_span_context().trace_id, "032x")
        assert current_trace_id() == expected
    # Outside any span the ambient context is invalid.
    assert current_trace_id() is None


def test_current_trace_id_from_explicit_span():
    telemetry._enabled = True
    provider = SDKTracerProvider()
    tracer = provider.get_tracer("test")
    span = tracer.start_span("probe")
    expected = format(span.get_span_context().trace_id, "032x")
    assert current_trace_id(span=span) == expected
    span.end()


def test_current_trace_id_tolerates_invalid_span():
    telemetry._enabled = True
    assert current_trace_id(span=trace_api.INVALID_SPAN) is None
    assert current_trace_id(span=object()) is None
