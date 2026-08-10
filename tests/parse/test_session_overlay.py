"""Session overlay merge — a run submitting its own workflow config.

Distinct from ``merge_raw_configs`` (see ``test_helpers``), which unions every
section and errors on duplicate names. An overlay must be able to *replace* what
the base declared, because the run is choosing a workflow rather than adding to
one.
"""

from __future__ import annotations

import pytest

from kaboo_workflows import load_session_config, parse_config_sources
from kaboo_workflows.config.loaders.helpers import merge_session_overlay
from kaboo_workflows.exceptions import ConfigurationError

BASE = {
    "models": {"default": {"provider": "openai", "model_id": "gpt-4o"}},
    "mcp_clients": {"platform": {"url": "https://tools.internal/mcp"}},
    "agents": {"assistant": {"system_prompt": "base"}},
    "entry": "assistant",
    "log_level": "WARNING",
}


def test_overlay_replaces_agents_rather_than_adding_to_them():
    merged = merge_session_overlay(
        BASE, {"agents": {"analyst": {"system_prompt": "new"}}, "entry": "analyst"}
    )
    assert list(merged["agents"]) == ["analyst"]
    assert merged["entry"] == "analyst"


def test_overlay_replaces_orchestrations():
    base = {**BASE, "orchestrations": {"old": {"type": "swarm", "agents": ["assistant"]}}}
    merged = merge_session_overlay(base, {"orchestrations": {"new": {"type": "swarm"}}})
    assert list(merged["orchestrations"]) == ["new"]


def test_models_and_clients_union_over_the_base():
    merged = merge_session_overlay(
        BASE,
        {
            "models": {"fast": {"provider": "openai", "model_id": "gpt-4o-mini"}},
            "mcp_clients": {"extra": {"url": "https://tools.internal/other"}},
        },
    )
    assert sorted(merged["models"]) == ["default", "fast"]
    assert sorted(merged["mcp_clients"]) == ["extra", "platform"]


def test_overlay_wins_a_name_clash_in_a_unioned_section():
    merged = merge_session_overlay(
        BASE, {"models": {"default": {"provider": "anthropic", "model_id": "claude"}}}
    )
    assert merged["models"]["default"]["provider"] == "anthropic"


def test_singletons_are_last_wins():
    merged = merge_session_overlay(
        BASE, {"log_level": "DEBUG", "runtime": {"persist_session_state": False}}
    )
    assert merged["log_level"] == "DEBUG"
    assert merged["runtime"] == {"persist_session_state": False}


def test_sections_the_overlay_omits_are_inherited():
    merged = merge_session_overlay(BASE, {"agents": {"a": {}}, "entry": "a"})
    assert merged["models"] == BASE["models"]
    assert merged["log_level"] == "WARNING"


def test_the_base_is_never_mutated():
    """One parsed base is shared by every concurrent run, so this is load-bearing."""
    merge_session_overlay(
        BASE,
        {
            "agents": {"other": {}},
            "models": {"fast": {"provider": "openai", "model_id": "m"}},
        },
    )
    assert list(BASE["agents"]) == ["assistant"]
    assert list(BASE["models"]) == ["default"]


def test_nested_overlay_values_are_not_shared_with_the_result():
    overlay = {"agents": {"a": {"tools": ["x"]}}, "entry": "a"}
    merged = merge_session_overlay(BASE, overlay)
    merged["agents"]["a"]["tools"].append("y")
    assert overlay["agents"]["a"]["tools"] == ["x"]


# ── what an overlay may not declare ──────────────────────────────────────────


def test_rejects_mcp_servers():
    with pytest.raises(ConfigurationError, match="mcp_servers"):
        merge_session_overlay(BASE, {"mcp_servers": {"local": {"command": ["uvx", "srv"]}}})


def test_rejects_a_client_that_would_spawn_a_process():
    with pytest.raises(ConfigurationError, match="command"):
        merge_session_overlay(BASE, {"mcp_clients": {"evil": {"command": ["sh", "-c", "x"]}}})


def test_rejects_a_client_url_off_the_allowlist():
    with pytest.raises(ConfigurationError, match="exfil.example"):
        merge_session_overlay(
            BASE,
            {"mcp_clients": {"leak": {"url": "https://exfil.example/mcp"}}},
            allowed_mcp_hosts=["tools.internal"],
        )


def test_allows_a_client_url_on_the_allowlist():
    merged = merge_session_overlay(
        BASE,
        {"mcp_clients": {"more": {"url": "https://Tools.Internal:8443/mcp"}}},
        allowed_mcp_hosts=["tools.internal"],
    )
    assert "more" in merged["mcp_clients"]


def test_no_allowlist_means_no_host_check():
    merged = merge_session_overlay(BASE, {"mcp_clients": {"any": {"url": "https://any.host/mcp"}}})
    assert "any" in merged["mcp_clients"]


def test_a_client_naming_a_base_server_is_left_alone():
    """``server:`` can only name a server the host itself declared."""
    merged = merge_session_overlay(
        BASE,
        {"mcp_clients": {"via_server": {"server": "local"}}},
        allowed_mcp_hosts=["tools.internal"],
    )
    assert merged["mcp_clients"]["via_server"] == {"server": "local"}


# ── load_session_config: the whole per-run path ──────────────────────────────


BASE_YAML = """
models:
  default:
    provider: openai
    model_id: gpt-4o
agents:
  assistant:
    system_prompt: base assistant
entry: assistant
"""


def test_submitted_config_replaces_the_workflow_and_keeps_shared_models():
    base_raw = parse_config_sources(BASE_YAML)
    config = load_session_config(
        base_raw,
        """
        agents:
          analyst:
            model: default
            system_prompt: submitted analyst
        entry: analyst
        """,
    )
    assert list(config.agents) == ["analyst"]
    assert config.agents["analyst"].system_prompt == "submitted analyst"
    assert config.entry == "analyst"
    # The base's model is still there to be referenced by name.
    assert "default" in config.models


def test_no_overlay_validates_the_base_alone():
    base_raw = parse_config_sources(BASE_YAML)
    assert load_session_config(base_raw).entry == "assistant"


def test_a_dangling_reference_in_a_submitted_config_names_what_is_available():
    base_raw = parse_config_sources(BASE_YAML)
    with pytest.raises(ConfigurationError) as exc:
        load_session_config(
            base_raw,
            """
            agents:
              analyst:
                mcp: [nope]
            entry: analyst
            """,
        )
    assert "nope" in str(exc.value)


def test_the_overlay_interpolates_from_this_process_environment(monkeypatch):
    """So a config authored in a database never has to hold the secret itself."""
    monkeypatch.setenv("TEST_OVERLAY_PROMPT", "from the environment")
    base_raw = parse_config_sources(BASE_YAML)
    config = load_session_config(
        base_raw,
        """
        agents:
          analyst:
            system_prompt: ${TEST_OVERLAY_PROMPT}
        entry: analyst
        """,
    )
    assert config.agents["analyst"].system_prompt == "from the environment"


def test_a_submitted_document_is_never_probed_as_a_file_path():
    """A real config is long and full of slashes; neither may look like a path.

    The path probe used to run first, so a config over the OS filename limit
    raised ENAMETOOLONG and one containing a URL was reported as a missing file.
    """
    overlay = "agents:\n  analyst:\n    system_prompt: " + "x" * 5000
    overlay += "\nmcp_clients:\n  gateway:\n    url: http://gateway.internal/mcp\nentry: analyst"

    config = load_session_config(parse_config_sources(BASE_YAML), overlay)

    assert config.entry == "analyst"
    assert str(config.mcp_clients["gateway"].url) == "http://gateway.internal/mcp"


def test_a_mistyped_config_path_is_still_reported_as_a_missing_file():
    with pytest.raises(FileNotFoundError):
        parse_config_sources("configs/does-not-exist.yaml")


def test_one_base_serves_repeated_runs_unchanged():
    base_raw = parse_config_sources(BASE_YAML)
    first = load_session_config(base_raw, "agents:\n  a:\n    system_prompt: one\nentry: a")
    second = load_session_config(base_raw, "agents:\n  b:\n    system_prompt: two\nentry: b")
    assert list(first.agents) == ["a"]
    assert list(second.agents) == ["b"]
    assert load_session_config(base_raw).entry == "assistant"
