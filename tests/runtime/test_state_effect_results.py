"""What a state effect's mapper receives as the tool's result.

An MCP tool can answer the model in prose and the program in structured content;
the mapper gets the structured content. Without it, a JSON text block is parsed
and any other text is passed as it is.
"""

from __future__ import annotations

from kaboo_workflows.hooks.state_effects import _result_value


def test_structured_content_wins_over_a_readable_text_block() -> None:
    result = {
        "status": "success",
        "content": [{"text": "applied: d1 is now v3."}],
        "structuredContent": {"status": "applied", "doc": "d1", "version": 3},
    }
    assert _result_value(result) == {"status": "applied", "doc": "d1", "version": 3}


def test_a_json_text_block_is_parsed_without_structured_content() -> None:
    assert _result_value({"content": [{"text": '{"id": "q_7"}'}]}) == {"id": "q_7"}


def test_other_text_is_passed_as_it_is() -> None:
    assert _result_value({"content": [{"text": "saved"}]}) == "saved"
