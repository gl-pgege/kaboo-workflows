"""Schema normalization for the reference/attachment config surface.

``attachments:`` accepts shorthand (``none``/``reference``/``inline``, booleans)
or a full mapping, normalized to :class:`AgentAttachmentsDef`. The global
``AppConfig.attachments`` carries the default policy + tool flag.
"""

from __future__ import annotations

import pytest

from kaboo_workflows.config.schema import AgentAttachmentsDef, AttachmentsDef
from tests.factories import agent_def, app_config


def test_default_agent_attachments_is_none_meaning_inherit():
    assert agent_def().attachments is None


@pytest.mark.parametrize(
    ("value", "enabled", "inline"),
    [
        ("none", False, False),
        (False, False, False),
        ("reference", True, False),
        ("inline", True, True),
        (True, True, True),
    ],
)
def test_shorthand_normalizes_to_agent_attachments_def(value, enabled, inline):
    ad = agent_def(attachments=value)
    assert isinstance(ad.attachments, AgentAttachmentsDef)
    assert ad.attachments.enabled is enabled
    assert ad.attachments.inline is inline


def test_mapping_form_is_accepted():
    ad = agent_def(attachments={"inline": True})
    assert isinstance(ad.attachments, AgentAttachmentsDef)
    assert ad.attachments.enabled is True
    assert ad.attachments.inline is True


def test_invalid_string_is_rejected():
    with pytest.raises(ValueError, match="Invalid attachments"):
        agent_def(attachments="sometimes")


def test_global_attachments_defaults():
    cfg = app_config()
    assert isinstance(cfg.attachments, AttachmentsDef)
    assert cfg.attachments.default == "reference"
    assert cfg.attachments.tool is True


def test_global_attachments_overridable():
    cfg = app_config(attachments=AttachmentsDef(default="none", tool=False))
    assert cfg.attachments.default == "none"
    assert cfg.attachments.tool is False
