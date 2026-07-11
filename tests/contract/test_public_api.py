"""Completeness gate for the public API surface.

A *reviewed decision* guard (like ``test_shape.py``): it locks in the invariant
that every publicly-importable symbol is resolvable, documented, and rendered on
a mkdocstrings page. It does not gap-fill — at the time of writing every public
symbol already has a Google-style docstring; this test keeps it that way.

To intentionally make a new subpackage public, add its dotted name to
``PUBLIC_MODULES`` and create a matching ``docs/api/<slug>.md`` autodoc stub.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS_API = ROOT / "docs" / "api"

# The full public surface users import from. Every entry must define ``__all__``
# and have a matching ``docs/api/<slug>.md`` autodoc page.
PUBLIC_MODULES = [
    "kaboo_workflows",
    "kaboo_workflows.adapters",
    "kaboo_workflows.config",
    "kaboo_workflows.hooks",
    "kaboo_workflows.mcp",
    "kaboo_workflows.tools",
    "kaboo_workflows.converters",
    "kaboo_workflows.renderers",
    "kaboo_workflows.types",
]


def _slug(module_name: str) -> str:
    """Return the docs/api page slug for a module (top-level keeps its full name)."""
    return "kaboo_workflows" if module_name == "kaboo_workflows" else module_name.split(".")[-1]


def _needs_docstring(obj: object) -> bool:
    """Only classes and functions/methods must carry a docstring.

    Constants, type aliases (``Union``/``Annotated``), and re-exported modules
    are legitimately doc-free at the symbol level.
    """
    return inspect.isclass(obj) or inspect.isroutine(obj)


@pytest.mark.parametrize("module_name", PUBLIC_MODULES)
def test_public_module_defines_all(module_name: str) -> None:
    mod = importlib.import_module(module_name)
    assert hasattr(mod, "__all__"), f"{module_name} must define __all__"
    assert mod.__all__, f"{module_name}.__all__ must be non-empty"


@pytest.mark.parametrize("module_name", PUBLIC_MODULES)
def test_public_module_has_autodoc_page(module_name: str) -> None:
    page = DOCS_API / f"{_slug(module_name)}.md"
    assert page.exists(), (
        f"missing autodoc page {page.relative_to(ROOT)} for public module "
        f"{module_name} — add a mkdocstrings stub (`::: {module_name}`)"
    )


@pytest.mark.parametrize("module_name", PUBLIC_MODULES)
def test_public_symbols_resolve_and_are_documented(module_name: str) -> None:
    mod = importlib.import_module(module_name)
    for name in mod.__all__:
        assert hasattr(mod, name), f"{module_name}.__all__ names {name!r} but it is not importable"
        obj = getattr(mod, name)

        if not _needs_docstring(obj):
            continue

        doc = inspect.getdoc(obj)
        assert doc and doc.strip(), f"{module_name}.{name} has no docstring"

        if inspect.isclass(obj):
            for attr, member in vars(obj).items():
                if attr.startswith("_"):
                    continue
                if not inspect.isroutine(member):
                    continue
                member_doc = inspect.getdoc(member)
                assert member_doc and member_doc.strip(), (
                    f"{module_name}.{name}.{attr} (public method) has no docstring"
                )
