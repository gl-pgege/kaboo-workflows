# Releasing kaboo-workflows

Releases are driven by [Conventional Commits](https://www.conventionalcommits.org/)
and [commitizen](https://commitizen-tools.github.io/commitizen/), which picks the
version and writes the CHANGELOG. **Publishing itself is a local `twine upload`.**

> **The upload is the only irreversible step.** Everything up to and including
> `just release` is local and undoable; a version once on PyPI cannot be
> replaced, only yanked.

---

## 1. Credentials

`twine` reads `TWINE_PASSWORD` from the environment — a PyPI API token, kept in
your shell profile. No `TWINE_USERNAME` is needed: twine 7 resolves it to
`__token__` by itself for uploads to PyPI. Run the upload from a login shell so
the variable is present.

To rehearse against the test registry instead, `uv run just release-test-publish`
uploads to TestPyPI.

### About `.github/workflows/publish.yml`

That workflow publishes via PyPI Trusted Publishing on a `v*.*.*` tag push, and
it is how releases up to `v0.11.0` were made. It has not run since, and
`v0.12.0` onward were uploaded locally. It is still armed, so pushing a tag may
start a run; if one does and the version is already uploaded, PyPI rejects the
duplicate and the workflow simply fails. Nothing is at risk either way.

### Enable GitHub Pages (one-time)

The docs site deploys via [`.github/workflows/pages.yml`](.github/workflows/pages.yml).
A maintainer must enable it once: **Settings → Pages → Source = "GitHub Actions"**.
The workflow cannot self-enable Pages.

---

## 2. Pre-release gates (safe, run locally)

```bash
# clean, fully-synced environment
uv sync --all-groups --all-extras

# quality + unit + e2e + completeness + doc snippets
uv run just check
uv run just test
uv run just test-docs

# docs build (strict: mkdocstrings renders the full API, no broken refs)
uv run mkdocs build --strict

# build + metadata check (dry run — NO upload)
rm -rf dist/
uv build --out-dir dist/
uv run python -m twine check dist/*

# confirm py.typed shipped in the wheel (PEP 561)
python -c "import zipfile,glob; w=glob.glob('dist/*.whl')[0]; names=zipfile.ZipFile(w).namelist(); print('kaboo_workflows/py.typed' in names)"
```

All of the above must be green (`twine check` = PASSED, the last line prints
`True`) before proceeding.

---

## 3. Release flow (after explicit approval)

```bash
# 1. preview the bump (no changes written)
uv run just release-dry

# 2. bump version + CHANGELOG + create the vX.Y.Z tag (runs check + test first)
uv run just release

# 3. build the artifacts the tag describes, from a clean dist/
uv run just release-build
uv run python -m twine check dist/*

# 4. THE LIVE PUBLISH
uv run python -m twine upload dist/*

# 5. share the tag and the CHANGELOG commit
git push origin main --tags
```

Step 4 is what puts the release on PyPI. Step 5 only shares it; do it after the
upload succeeds, so a failed upload leaves no tag claiming a version that does
not exist.

---

## How versioning works

We follow **Semantic Versioning** (`MAJOR.MINOR.PATCH`); commit messages drive
the bump. `major_version_zero = true` keeps the API on `0.x` until a deliberate
1.0.

| Commit prefix | Bump | Example |
|---------------|------|---------|
| `fix:` | patch | `fix: handle empty tool name` |
| `feat:` | minor | `feat: add graph orchestration` |
| `feat!:` / `BREAKING CHANGE:` | major | `feat!: remove legacy API` |

Use `uv run just commit-files` for the interactive commit wizard, or commit
manually.

## Release candidates

```bash
uv run cz bump --prerelease rc
uv run just release-build && uv run python -m twine upload dist/*
git push origin main --tags
```

## Just commands

| Command | What it does |
|---------|-------------|
| `uv run just release-dry` | Preview next version + changelog |
| `uv run just release` | Bump, CHANGELOG, tag (runs check + test) |
| `uv run just release-build` | Build wheel + sdist into a clean `dist/` |
| `uv run python -m twine upload dist/*` | Publish to PyPI |
| `uv run just release-test-publish` | Upload to TestPyPI (dry-run registry) |
| `uv run just commit-files` | Interactive conventional commit |
