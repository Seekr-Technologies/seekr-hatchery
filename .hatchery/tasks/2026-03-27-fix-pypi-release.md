# Task: fix-pypi-release

**Status**: complete
**Branch**: hatchery/fix-pypi-release
**Created**: 2026-03-27 16:34

## Objective

Switch the PyPI publish step in `release.yml` from API-token auth to PyPI Trusted Publisher (OIDC), which was already configured on PyPI but never wired up in the workflow.

## Context

The project moved to PyPI Trusted Publisher. A GitHub environment named `pypi` was created, but `release.yml` still passed `secrets.PYPI_TOKEN` to `uv publish`. That token belongs to the user `lgrado`, who no longer has upload rights under the trusted-publisher model, producing a 403.

## Summary

**Root cause**: `publish-pypi` job was still using `uv publish --token "${{ secrets.PYPI_TOKEN }}"`. The old API token was for the user `lgrado`, not the trusted publisher, so PyPI rejected it.

**Fix** (`.github/workflows/release.yml`):
- Added `environment: pypi` to the `publish-pypi` job so it runs inside the protected GitHub environment, which is what PyPI's trusted publisher configuration points to.
- Added `permissions: id-token: write` so GitHub Actions can mint an OIDC token for the job.
- Replaced `uv publish --token "..."` with `uv publish` — `uv` detects the OIDC token automatically when no `--token` flag is present.

**Gotcha**: The GitHub environment must match exactly what was registered on PyPI's trusted publisher settings (name is case-sensitive). The environment here is `pypi` — verify it matches the PyPI configuration if the error recurs.
