# Task: user-local-config

**Status**: complete
**Branch**: include-home-expansion
**Created**: 2026-10-23 08:26
**Completed**: 2026-10-23

## Objective

Allow repositories to keep a shared, committed Docker sandbox configuration
while each user can add machine-specific mounts, environment variables, and
other settings without committing them.

## Summary

Hatchery now supports a main-checkout `.hatchery/docker.local.yaml` overlay on
the committed `.hatchery/docker.yaml`. The overlay uses the same schema:
mappings merge recursively, lists append, and local scalar values override
shared values. List ordering makes later local environment entries win when
the same variable is declared more than once.

`hatchery sandbox edit --local` creates the overlay from a minimal template,
validates it together with the shared config, and adds its exact path to the
repository-local `.git/info/exclude`. It cannot be combined with `--task` or
`--commit`; one local file applies to sandbox shells and every task worktree.
Normal commit-mode task and sandbox setup also preserves this exclusion. In
no-commit mode the existing exclusion of the whole `.hatchery/` directory is
sufficient.

Task launches explicitly load the shared config from the task checkout and the
local overlay from the main checkout. Initial include processing reads the
effective main-checkout configuration, while the post-worktree reload remains
checkout-relative to avoid resolving local relative paths against a task
worktree.

Key files:
- `src/seekr_hatchery/docker.py` — layered parsing, merging, validation, and
  local-template creation.
- `src/seekr_hatchery/sessions.py` / `cli.py` — exclusion, edit command, and
  task-worktree integration.
- `src/seekr_hatchery/resources/docker.local.yaml.template` — minimal local
  overlay template.
- `README.md` and `docker.yaml.template` — user-facing behavior and usage.
- Tests cover merge precedence, alternate main-checkout overlays, invalid local
  config attribution, edit constraints, and Git exclusion.

Validation: Ruff formatting and linting pass; all 577 directly affected module
tests pass, and focused follow-up tests pass. The full 1,411-test run reached
the unrelated kubectl RBAC integration tests before the container's
`cryptography` extension terminated with an illegal-instruction error.
Package build succeeds and includes the new template resource.

Follow-up: `include` paths now expand `~` consistently before path resolution.
This applies to `docker.yaml`, `docker.local.yaml`, post-edit include discovery,
and quoted `--include`, `--include-rw`, and `--include-ro` arguments. A custom
Click path type performs expansion before existence validation, while
`sessions.resolve_include_path()` centralizes expansion and repository-relative
resolution. README and template documentation describe all three supported
forms: absolute, repository-relative, and home-relative. Ruff passes and the
356 CLI/session tests pass.
