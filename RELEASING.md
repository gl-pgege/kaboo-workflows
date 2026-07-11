# Releasing kaboo-workflows

Releases are driven by [Conventional Commits](https://www.conventionalcommits.org/)
and automated via [commitizen](https://commitizen-tools.github.io/commitizen/) +
GitHub Actions. Publishing to PyPI uses **Trusted Publishing** (OIDC) — no API
tokens are stored anywhere.

> **DRY-RUN ONLY by default.** Pushing a version tag is the ONLY action that
> publishes to PyPI, and it must wait for explicit maintainer approval. Everything
> below up to and including step 3 of the release flow is safe and non-publishing.

---

## 1. One-time PyPI Trusted Publisher setup (maintainer, cannot be automated)

1. Log in to <https://pypi.org>. If the project `kaboo-workflows` does not exist
   yet, use **Publishing → Add a pending publisher** (creates the project on first
   publish). Otherwise open the project → **Manage → Publishing**.
2. Add a **GitHub Actions** Trusted Publisher with:
   - **PyPI Project Name**: `kaboo-workflows`
   - **Owner**: `gl-pgege`
   - **Repository name**: `kaboo-workflows`
   - **Workflow name**: `publish.yml`
   - **Environment name**: `release` (matches `environment: release` in
     [`.github/workflows/publish.yml`](.github/workflows/publish.yml))
3. In the GitHub repo, create the `release` environment (**Settings →
   Environments**) and optionally add **required reviewers** so a human must
   approve before the publish job runs.
4. **(Recommended) TestPyPI dry run.** Repeat the pending-publisher setup on
   <https://test.pypi.org>, then validate the flow with
   `uv run just release-test-publish` before ever touching production PyPI.

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

# 3. THE ONLY LIVE-PUBLISH ACTION — push main + tags to trigger publish.yml
git push origin main --tags
```

`publish.yml` triggers on tags matching `v[0-9]+.[0-9]+.[0-9]+` (plus `.post*` /
`rc*`). It runs the CI gate, builds the wheel + sdist, publishes to PyPI via the
Trusted Publisher, and creates a GitHub Release from the CHANGELOG.

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
git push origin main --tags   # (only after approval)
```

## Just commands

| Command | What it does |
|---------|-------------|
| `uv run just release-dry` | Preview next version + changelog |
| `uv run just release` | Bump, CHANGELOG, tag (runs check + test) |
| `uv run just release-build` | Build wheel + sdist locally |
| `uv run just release-test-publish` | Upload to TestPyPI (dry-run registry) |
| `uv run just commit-files` | Interactive conventional commit |
