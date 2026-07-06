"""The single import resolver — load_object over module and file specs."""

from __future__ import annotations

import pytest

from kaboo_workflows.exceptions import ImportResolutionError
from kaboo_workflows.utils import load_object


def test_loads_object_from_module_spec():
    obj = load_object("kaboo_workflows.hooks:StopGuard")
    assert obj.__name__ == "StopGuard"


def test_loads_object_from_file_spec(tmp_path):
    mod = tmp_path / "thing.py"
    mod.write_text("VALUE = 42\n")
    assert load_object(f"{mod}:VALUE") == 42


def test_spec_without_colon_raises_import_resolution_error():
    with pytest.raises(ImportResolutionError):
        load_object("kaboo_workflows.hooks")


def test_missing_module_raises_import_resolution_error():
    with pytest.raises(ImportResolutionError):
        load_object("no.such.module:Thing")


def test_missing_attribute_raises_import_resolution_error():
    with pytest.raises(ImportResolutionError):
        load_object("kaboo_workflows.hooks:DoesNotExist")


def test_missing_file_attribute_raises_import_resolution_error(tmp_path):
    mod = tmp_path / "thing.py"
    mod.write_text("VALUE = 1\n")
    with pytest.raises(ImportResolutionError):
        load_object(f"{mod}:MISSING")
