"""Guard that the AI-native layer never drifts from its sources.

``llms.txt`` and ``llms-full.txt`` are generated from README + docs by
``scripts/gen_llms.py``. If either file is stale relative to the sources, this
test fails with the exact fix: ``uv run just docs-llms``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location("gen_llms", ROOT / "scripts" / "gen_llms.py")
assert _spec and _spec.loader
gen_llms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen_llms)


def test_llms_full_txt_is_current() -> None:
    expected = gen_llms.build_llms_full()
    actual = (ROOT / "llms-full.txt").read_text(encoding="utf-8")
    assert actual == expected, "llms-full.txt is stale — run `uv run just docs-llms`"


def test_llms_txt_is_current() -> None:
    expected = gen_llms.build_llms_txt()
    actual = (ROOT / "llms.txt").read_text(encoding="utf-8")
    assert actual == expected, "llms.txt is stale — run `uv run just docs-llms`"
