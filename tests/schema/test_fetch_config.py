"""attachments fetch config + runtime toggle validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kaboo_workflows.config.schema import AppConfig, AttachmentsDef, RuntimeDef

_AGENTS = {"a": {"model": "m"}}


def test_attachments_fetch_fields_default_off():
    d = AttachmentsDef()
    assert d.base_url is None
    assert d.authorization is None
    assert d.content_url_template is None


def test_attachments_accepts_valid_fetch_config():
    d = AttachmentsDef(
        base_url="https://api.example.com",
        authorization="forwarded_props:runToken",
        content_url_template="/attachments/{id}/content",
    )
    assert d.authorization == "forwarded_props:runToken"
    assert AttachmentsDef(authorization="env:MY_TOKEN").authorization == "env:MY_TOKEN"


def test_attachments_rejects_unknown_authorization_scheme():
    with pytest.raises(ValidationError, match="forwarded_props"):
        AttachmentsDef(authorization="bearer:abc")


def test_attachments_rejects_template_without_id_placeholder():
    with pytest.raises(ValidationError, match="placeholder"):
        AttachmentsDef(content_url_template="/attachments/content")


def test_runtime_overrides_default_off():
    assert RuntimeDef().allow_invocation_overrides is False
    config = AppConfig.model_validate({"entry": "a", "agents": _AGENTS})
    assert config.runtime.allow_invocation_overrides is False


def test_runtime_overrides_opt_in():
    config = AppConfig.model_validate(
        {"entry": "a", "agents": _AGENTS, "runtime": {"allow_invocation_overrides": True}}
    )
    assert config.runtime.allow_invocation_overrides is True
