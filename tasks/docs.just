# regenerate llms.txt + llms-full.txt from README + docs
[group('docs')]
docs-llms:
    uv run python scripts/gen_llms.py

# build the MkDocs Material site (strict: fails on broken refs)
[group('docs')]
docs-build:
    uv run --group docs mkdocs build --strict

# serve the docs site locally with live reload
[group('docs')]
docs-serve:
    uv run --group docs mkdocs serve
