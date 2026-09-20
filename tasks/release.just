# ---------------------------------------------------------------------------
# Release tasks
#
# Normal release flow:
#   1. uv run just release-dry      -> preview version bump + changelog diff
#   2. uv run just release          -> bump version, update CHANGELOG, create tag
#   3. uv run just release-build    -> clean dist/
#   4. uv run python -m twine upload dist/*   -> publish (the live step)
#   5. git push origin main --tags  -> share the tag, after the upload succeeds
#
# See RELEASING.md. publish.yml is dormant; releases are uploaded locally.
# ---------------------------------------------------------------------------

# Preview the next version bump (no changes written)
[group('release')]
release-dry:
    uv run cz bump --dry-run

# Bump version, update CHANGELOG, and create a git tag
[group('release')]
release: check test
    uv run cz bump
    @echo ""
    @echo "✅  Version bumped. Publish with:"
    @echo "      uv run just release-build"
    @echo "      uv run python -m twine check dist/*"
    @echo "      uv run python -m twine upload dist/*"
    @echo "      git push origin main --tags"

# Build distribution artifacts (local check)
[group('release')]
release-build:
    rm -rf dist/
    uv build --out-dir dist/
    @echo ""
    @ls dist/

# Publish to TestPyPI (safe dry-run against the test registry)
[group('release')]
release-test-publish:
    uv run python -m twine upload --repository testpypi dist/*

# Show the next version that commitizen would pick
[group('release')]
release-next:
    uv run cz bump --dry-run 2>&1 | grep -E "tag to create|bump" || echo "(no releasable commits since last tag)"
