"""Schema validation contracts — good config validates, bad config raises typed errors.

Asserts on the error *type* (and the offending identifier), never on message prose.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kaboo_workflows.config.loaders import load_config
from kaboo_workflows.config.schema import AppConfig, MCPClientDef, OrchestrationDef
from kaboo_workflows.exceptions import SchemaValidationError
from tests.factories import agent_def, app_config

# ── AppConfig cross-field validators ───────────────────────────────────────


def test_valid_minimal_config_validates():
    config = app_config()
    assert config.entry == "a"


def test_entry_referencing_unknown_node_raises():
    with pytest.raises(ValidationError, match="ghost"):
        AppConfig(agents={"a": agent_def()}, entry="ghost")


def test_name_collision_across_agents_and_orchestrations_raises():
    from tests.factories import delegate_orchestration

    with pytest.raises(ValidationError, match="dupe"):
        AppConfig(
            agents={"dupe": agent_def(), "helper": agent_def()},
            orchestrations={"dupe": delegate_orchestration("helper", {"helper": "d"})},
            entry="dupe",
        )


# ── MCPClientDef connection-mode validator ─────────────────────────────────


def test_mcp_client_requires_exactly_one_connection_mode_none_set():
    with pytest.raises(ValidationError):
        MCPClientDef()


def test_mcp_client_rejects_multiple_connection_modes():
    with pytest.raises(ValidationError):
        MCPClientDef(server="s", url="http://x")


def test_mcp_client_accepts_single_connection_mode():
    assert MCPClientDef(server="s").server == "s"


# ── Orchestration discriminated union ──────────────────────────────────────


@pytest.mark.parametrize(
    ("mode", "extra"),
    [
        ("delegate", {"entry_name": "a", "connections": [{"agent": "b", "description": "d"}]}),
        ("swarm", {"entry_name": "a", "agents": ["a", "b"]}),
        ("graph", {"entry_name": "a", "edges": [{"from": "a", "to": "b"}]}),
    ],
)
def test_orchestration_union_dispatches_on_mode(mode, extra):
    from pydantic import TypeAdapter

    orch = TypeAdapter(OrchestrationDef).validate_python({"mode": mode, **extra})
    assert orch.mode == mode


def test_orchestration_unknown_mode_raises():
    from pydantic import TypeAdapter

    with pytest.raises(ValidationError):
        TypeAdapter(OrchestrationDef).validate_python({"mode": "quantum", "entry_name": "a"})


def test_graph_edge_accepts_from_to_aliases():
    from kaboo_workflows.config.schema import GraphEdgeDef

    edge = GraphEdgeDef(**{"from": "a", "to": "b"})
    assert (edge.from_agent, edge.to_agent) == ("a", "b")


# ── HistoryDef: the summary backstop ───────────────────────────────────────


@pytest.mark.parametrize(
    ("history", "missing"),
    [
        pytest.param({"keep": "masked", "summarize_over": 5000}, "summarize_model", id="no-model"),
        pytest.param(
            {"summarize_over": 5000, "summarize_model": "cheap"}, "keep", id="full-history"
        ),
    ],
)
def test_summarize_over_needs_a_masked_history_and_a_model(history, missing):
    from kaboo_workflows.config.schema import HistoryDef

    with pytest.raises(ValidationError, match=missing):
        HistoryDef(**history)


def test_a_masked_history_with_a_summary_validates():
    from kaboo_workflows.config.schema import HistoryDef

    history = HistoryDef(keep="masked", summarize_over=5000, summarize_model="cheap")

    assert (history.keep, history.summarize_over) == ("masked", 5000)


# ── ModelDef: a bedrock model's client_config ──────────────────────────────


def test_bedrock_client_config_accepts_timeouts_and_retries():
    from kaboo_workflows.config.schema import ModelDef

    model = ModelDef(
        provider="bedrock",
        model_id="m",
        params={
            "client_config": {
                "read_timeout": 300,
                "connect_timeout": 5,
                "retries": {"max_attempts": 4, "mode": "adaptive"},
            }
        },
    )

    assert model.params["client_config"]["retries"] == {"max_attempts": 4, "mode": "adaptive"}


@pytest.mark.parametrize(
    "client_config",
    [
        {"max_pool_connections": 10},
        {"retries": {"total_max_attempts": 3}},
        {"retries": {"mode": "eventually"}},
        {"read_timeout": 0},
    ],
)
def test_bedrock_client_config_rejects_unknown_keys_and_bad_values(client_config):
    from kaboo_workflows.config.schema import ModelDef

    with pytest.raises(ValidationError, match="client_config"):
        ModelDef(provider="bedrock", model_id="m", params={"client_config": client_config})


def test_bad_bedrock_client_config_in_yaml_is_a_schema_validation_error():
    from tests.factories import yaml_config

    yaml_text = yaml_config(
        """
        models:
          fast:
            provider: bedrock
            model_id: m
            params:
              client_config: {read_timeot: 30}
        agents:
          a: {model: fast}
        entry: a
        """
    )
    with pytest.raises(SchemaValidationError, match="read_timeot"):
        load_config(yaml_text)


# ── load_config surfaces schema failures as the typed subclass ─────────────


def test_load_config_wraps_schema_failure_as_schema_validation_error():
    # 'agents' must be a mapping; a list is a schema violation.
    with pytest.raises(SchemaValidationError):
        load_config("agents: [not, a, mapping]\nentry: a")
