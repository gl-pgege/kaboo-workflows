"""The service describes itself to AG-UI clients as ``AgentCapabilities``."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from kaboo_workflows.adapters.agui import agent_capabilities, create_agui_app
from kaboo_workflows.config import AppConfig, validate_raw_config

SCRIPTED = {"provider": "kaboo_workflows.testing:ScriptedModel", "model_id": "scripted"}
GATE = Path(__file__).parents[1] / "e2e" / "configs" / "approval_gate.yaml"


def _config(**extra: object) -> AppConfig:
    return validate_raw_config(
        {
            "agents": {
                "lead": {"model": SCRIPTED, "system_prompt": "Lead."},
                "worker": {
                    "model": SCRIPTED,
                    "system_prompt": "Work.",
                    "interrupt": {"tools": ["delete_*"]},
                },
            },
            "entry": "lead",
            **extra,
        }
    )


def test_a_plain_config_declares_no_orchestration_but_its_approvals() -> None:
    capabilities = agent_capabilities(_config()).model_dump(by_alias=True, exclude_none=True)
    assert capabilities["identity"]["name"] == "lead"
    assert capabilities["multiAgent"] == {
        "supported": False,
        "delegation": False,
        "handoffs": False,
    }
    assert capabilities["humanInTheLoop"] == {
        "supported": True,
        "interrupts": True,
        "approvals": True,
    }
    assert capabilities["custom"] == {"perRunWorkflows": False}


def test_a_delegate_config_declares_delegation() -> None:
    config = _config(
        orchestrations={
            "main": {
                "mode": "delegate",
                "entry_name": "lead",
                "connections": [{"agent": "worker", "description": "Works."}],
            }
        },
    )
    multi = agent_capabilities(config).model_dump(by_alias=True, exclude_none=True)["multiAgent"]
    assert multi == {"supported": True, "delegation": True, "handoffs": False}


def test_per_run_workflows_declare_what_any_run_may_bring() -> None:
    capabilities = agent_capabilities(_config(), per_run_configs=True).model_dump(
        by_alias=True, exclude_none=True
    )
    assert capabilities["multiAgent"] == {"supported": True, "delegation": True, "handoffs": True}
    assert capabilities["custom"] == {"perRunWorkflows": True}


def test_the_service_serves_its_capabilities_beside_the_endpoint() -> None:
    with TestClient(create_agui_app(GATE)) as client:
        response = client.get("/invocations/capabilities")
    assert response.status_code == 200
    assert response.json()["identity"]["type"] == "kaboo-workflows"
