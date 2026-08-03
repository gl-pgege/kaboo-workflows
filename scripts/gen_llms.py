#!/usr/bin/env python
"""Generate ``llms.txt`` and ``llms-full.txt`` from README + docs.

- ``llms-full.txt`` — a single concatenation of ``README.md`` followed by every
  ``docs/**/*.md`` (sorted), each prefixed with a ``# <relative path>`` header,
  for whole-context ingestion by an LLM.
- ``llms.txt`` — the llmstxt.org format: an H1, a blockquote summary, then curated
  sections of links (Documentation, Configuration, Workflows, API reference,
  Examples, and the sibling repos).

Both files are fully generated — do not hand-edit them. Regenerate with
``uv run just docs-llms``. A staleness test
(``tests/contract/test_llms.py``) fails CI if they drift from the sources.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
DOCS = ROOT / "docs"
REPO = "https://github.com/gl-pgege/kaboo-workflows/blob/main"
SITE = "https://gl-pgege.github.io/kaboo-workflows/"

SUMMARY = (
    "YAML-driven multi-agent orchestration for strands-agents with native AG-UI / "
    "CopilotKit support. Describe models, agents, tools, hooks, MCP servers, and "
    "nested delegate/swarm/graph orchestrations in YAML; load() returns live, fully "
    "wired strands objects and kaboo-serve serves them as AG-UI Server-Sent Events."
)


def _docs_files() -> list[Path]:
    """Every markdown file under docs/, sorted by relative path."""
    return sorted(DOCS.rglob("*.md"), key=lambda p: p.relative_to(ROOT).as_posix())


def _title(path: Path) -> str:
    """First ``# `` heading in the file, else the file stem."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem


def build_llms_full() -> str:
    """Concatenate README + every docs page with path headers."""
    parts: list[str] = [f"# README.md\n\n{README.read_text(encoding='utf-8').rstrip()}\n"]
    for path in _docs_files():
        rel = path.relative_to(ROOT).as_posix()
        parts.append(f"# {rel}\n\n{path.read_text(encoding='utf-8').rstrip()}\n")
    return "\n".join(parts).rstrip() + "\n"


def _links(paths: list[Path]) -> list[str]:
    lines = []
    for path in paths:
        rel = path.relative_to(ROOT).as_posix()
        lines.append(f"- [{_title(path)}]({REPO}/{rel})")
    return lines


def build_llms_txt() -> str:
    """Build the llmstxt.org-format index."""
    docs = _docs_files()
    configuration = [p for p in docs if p.parent.name == "configuration"]
    workflows = [p for p in docs if p.parent.name == "workflows"]
    api = [p for p in docs if p.parent.name == "api"]
    top = [
        p
        for p in docs
        if p.parent == DOCS and p.name in {"index.md", "api-reference.md"}
    ]

    out: list[str] = ["# kaboo-workflows\n", f"> {SUMMARY}\n"]

    out.append("## Documentation\n")
    out.append(f"- [Documentation site]({SITE})")
    out.append(f"- [README]({REPO}/README.md)")
    out.extend(_links(top))
    out.append("")

    out.append("## Configuration\n")
    out.extend(_links(configuration))
    out.append("")

    out.append("## Workflows\n")
    out.extend(_links(workflows))
    out.append("")

    out.append("## API reference\n")
    out.extend(_links(api))
    out.append("")

    out.append("## Examples\n")
    out.append(f"- [All examples]({REPO}/examples)")
    out.append("")

    out.append("## The kaboo stack\n")
    out.append("- [kaboo-runtime](https://github.com/gl-pgege/kaboo-runtime) — CopilotKit runtime persistence")
    out.append("- [kaboo-react](https://github.com/gl-pgege/kaboo-react) — agent-activity React UI")
    out.append("- [kaboo-workflows-demo](https://github.com/gl-pgege/kaboo-workflows-demo) — runnable end-to-end reference")
    out.append("- [The kaboo stack](https://gl-pgege.github.io/kaboo-docs/) — umbrella landing")

    return "\n".join(out).rstrip() + "\n"


def main() -> None:
    (ROOT / "llms-full.txt").write_text(build_llms_full(), encoding="utf-8")
    (ROOT / "llms.txt").write_text(build_llms_txt(), encoding="utf-8")
    print("wrote llms.txt + llms-full.txt")  # noqa: T201


if __name__ == "__main__":
    main()
