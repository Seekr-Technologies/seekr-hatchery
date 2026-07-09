# Task: fix-click-choice

**Status**: complete
**Branch**: hatchery/fix-click-choice
**Created**: 2026-07-09 13:58

## Objective

For the agent choice parameter, can we popualte it directly using the ALL_BACKENDS list in agents/__init__.py so we don't have to keep that list in sync in two places?

## Summary

The `--agent` option's `click.Choice` list was hardcoded as `["codex"]` in two
places in `cli.py` (the `new` and `chat` commands), duplicating information
already maintained in `agents/__init__.py` via `ALL_BACKENDS`.

**Changes:**
- `agents/__init__.py`: Added `ALL_BACKENDS` to `__all__` to make it part of the
  public API.
- `cli.py`: Added a module-level constant `AGENT_CHOICES = [b.kind.lower() for b
  in agent.ALL_BACKENDS]` and replaced both `click.Choice(["codex"], ...)` calls
  with `click.Choice(AGENT_CHOICES, ...)`.

**Key decision:** The `kind` attribute on backends is uppercase (e.g. `"CODEX"`),
but click choices use `case_sensitive=False`, so we lowercased the values for
cleaner help text. Adding a new backend to `ALL_BACKENDS` now automatically makes
it available as an `--agent` choice with no further changes needed.
