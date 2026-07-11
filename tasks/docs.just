# regenerate llms.txt + llms-full.txt from README + docs
[group('docs')]
docs-llms:
    uv run python scripts/gen_llms.py

# build the MkDocs Material site (strict: fails on broken refs)
[group('docs')]
docs-build:
    uv run --group docs mkdocs build --strict

# guard against markdown fence corruption leaking into the built site
[group('docs')]
docs-check-render: docs-build
    @echo "checking built HTML for fence corruption…"
    @if grep -rIl '```' site --include='*.html'; then echo "ERROR: literal \`\`\` in built HTML (broken code fence)"; exit 1; fi
    @if grep -rIlE 'python notest|\{\.python notest\}' site --include='*.html'; then echo "ERROR: fence info string leaked into rendered HTML"; exit 1; fi
    @echo "render OK"

# serve the docs site locally with live reload
[group('docs')]
docs-serve:
    uv run --group docs mkdocs serve
