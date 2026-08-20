"""The $${ escape yields a literal ${ without variable resolution."""

from __future__ import annotations

import pytest

from kaboo_workflows.config.interpolation import interpolate


def test_escaped_reference_is_left_literal():
    result = interpolate({"k": "cost is $${value:,.1f}B"}, variables={}, env={})
    assert result["k"] == "cost is ${value:,.1f}B"


def test_escape_and_real_reference_in_one_string():
    result = interpolate(
        {"k": "$${literal} and ${REAL}"}, variables={}, env={"REAL": "resolved"}
    )
    assert result["k"] == "${literal} and resolved"


def test_double_dollar_not_before_brace_is_untouched():
    result = interpolate({"k": "kill -9 $$ now"}, variables={}, env={})
    assert result["k"] == "kill -9 $$ now"


def test_escaped_cell_template_survives():
    text = "$${{cell:8f14e45f.total[0] | number 2}}"
    result = interpolate({"k": text}, variables={}, env={})
    assert result["k"] == "${{cell:8f14e45f.total[0] | number 2}}"


def test_unescaped_unknown_reference_still_raises():
    with pytest.raises(ValueError):
        interpolate({"k": "${DEFINITELY_NOT_SET}"}, variables={}, env={})
