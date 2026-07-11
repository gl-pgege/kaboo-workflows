# kaboo-workflows — Agent Instructions

This is **kaboo-workflows**: a declarative multi-agent orchestration library for
[strands-agents](https://github.com/strands-agents/harness-sdk). It reads YAML
configs and returns fully wired, plain `strands` objects — no wrappers, no
subclasses.

---

## Read the Skill First — MANDATORY

Before touching any code, load the skill for the area you are working in. Skills
are the authoritative source for the mental model, conventions, patterns,
dependency rules, and file placement. Everything library-specific lives there,
not here.

| Area | Skill to load |
|------|---------------|
| **Library source** (`src/kaboo_workflows/`) | `.kiro/skills/library-development/SKILL.md` + `.kiro/skills/library-development/references/project-map.md` |
| **Library tests** (`tests/`) | `.kiro/skills/library-testing/SKILL.md` + `.kiro/skills/library-testing/references/test-patterns.md` |

If you work on the library source, read the `library-development` skill; if you
work on tests, read the `library-testing` skill. They describe the target
standard — follow it, not whatever pattern happened to be written before the
skill existed.

---

## Core Principles — Apply Everywhere

When in doubt, apply these in order.

1. **Strands-first** — always check what `strands-agents` already provides
   (`.venv/lib/python*/site-packages/strands/`) before implementing anything.
   Use it directly; never re-implement what it exports.
2. **Thin wrapper** — translate YAML to strands objects, then get out of the way.
   Return plain strands objects, never a wrapper or subclass.
3. **Simple over clever** — the dullest solution that correctly solves the
   problem is the right one. Readable and maintainable beats terse.
4. **Transparency over performance** — prefer code that clearly shows what it
   does. Optimize only with measured evidence that it matters.
5. **Explicit over implicit** — no hidden magic, no auto-registration, no global
   singletons. Wire things by hand and make dependencies obvious.
6. **Composition over inheritance** — build big things from small, focused
   pieces that compose.
7. **Single responsibility** — each module, function, and resolver does one
   thing. When something grows a second job, split it.
8. **One-way dependencies** — the pipeline flows one direction; inner layers
   never import outward. The exact direction is defined in the skill.

---

## Behaviour Rules — Apply Everywhere

- **Smallest reasonable change.** Don't refactor unrelated code to land a
  feature. Touch only what the task requires.
- **Read before writing.** Before editing a file, read it. Before creating
  something new, read a sibling that plays the same role and match its shape.
- **No hardcoded secrets.** All credentials and sensitive config come from
  environment variables.
- **Comments explain what and why, never when or how something changed.** No
  temporal context ("recently refactored", "moved from …") in comments.
- **If you find something broken in the area you're working, fix it.** Don't
  leave broken or commented-out code behind.
- **Never add files or change code outside the scope of the task.**
- **Verify before done** — `uv run just check` then `uv run just test` (see the
  `check-and-test` skill).

---

## Commands

The authoritative dev loop (run from the repo root, `uv` required):

| Command | What it does |
|---------|--------------|
| `uv run just install` | Install deps + git hooks |
| `uv run just check` | Ruff format + lint, `ty` type check, Bandit |
| `uv run just test` | pytest + coverage (>=70% gate), incl. contract + e2e |
| `uv run just test-docs` | Execute the python fences embedded in README + docs |
| `uv run just docs-llms` | Regenerate `llms.txt` + `llms-full.txt` |
| `uv run just docs-build` | Build the MkDocs site with `--strict` |
| `uv run just commit-files` | Conventional Commit via commitizen |

---

## How-to recipes

Every change ends in the tests/docs that prove it (see Definition of Done):

- **Add a public symbol** — export it from the subpackage `__all__`, give it a
  Google-style docstring; it is auto-rendered by the module's `docs/api/*.md`
  page. `tests/contract/test_public_api.py` enforces both.
- **Add a new public subpackage** — add its dotted name to `PUBLIC_MODULES` in
  `tests/contract/test_public_api.py` and create `docs/api/<slug>.md` with a
  `::: kaboo_workflows.<slug>` stub, then add it to `mkdocs.yml` nav.
- **Add an example** — create `examples/NN_<slug>/` with a `config.yaml` (and
  optional `main.py` + `README.md`). It is auto load-tested by
  `tests/pipeline/test_examples.py`.
- **Add a workflow guide** — write `docs/workflows/<slug>.md`, add it to the
  `mkdocs.yml` nav, and back it with a passing e2e test in `tests/e2e/` (add a
  scripted `tests/e2e/configs/*.yaml` if a new shape is needed — the config
  globber test drives every one).
- **Change docs** — if README/docs changed, run `uv run just docs-llms` so the
  AI layer stays current (`tests/contract/test_llms.py` enforces this).

---

## Definition of Done (AI)

- [ ] `uv run just check` clean
- [ ] `uv run just test` green (incl. `tests/contract/*` + `tests/e2e/*`)
- [ ] `uv run just test-docs` green (all doc fences execute or are `notest`)
- [ ] `uv run just docs-build` clean (mkdocstrings renders, no broken refs)
- [ ] new public symbol has a Google-style docstring + an autodoc page
- [ ] new example has a passing load/e2e test; new workflow guide has a proof
- [ ] `llms.txt` / `llms-full.txt` regenerated if docs changed
- [ ] smallest reasonable change; no secrets; no out-of-scope edits

---

## Related

- [kaboo-workflows-demo](https://github.com/gl-pgege/kaboo-workflows-demo) — the
  runnable, end-to-end reference that serves this library as AG-UI SSE.
- [kaboo-runtime](https://github.com/gl-pgege/kaboo-runtime) — CopilotKit runtime
  persistence.
- [kaboo-react](https://github.com/gl-pgege/kaboo-react) — agent-activity React UI.
- [The kaboo stack](https://gl-pgege.github.io/kaboo-docs/) — umbrella landing.
