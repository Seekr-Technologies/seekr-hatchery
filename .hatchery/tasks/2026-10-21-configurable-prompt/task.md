# Task: configurable-prompt

**Status**: complete
**Branch**: configurable-prompt
**Created**: 2026-10-21 09:05

## Objective

Please update the hatchery system prompt to be user-configurable.

  - We need a cli command to edit: `hatchery edit`...
  - it should live in the user ~/.hatchery dir
  - it should be _seeded_ from the repo copy
  - we also want a command to re-seed (overwrite user version), if they want an update

  Also, please update the current system prompt. It is a bit too constrictive. Especially in the pi harness, it results in agents asking for permission to do everything.

  Example, I am discussing a PR update with an agent. I say "record that to a file".

  They then present me with a plan to "record to file X".

  they should just do it. The plan workflow is for the beginning, or for BIG things

## Context

Configuration UX follows the scope-option pattern used by tools such as Git,
pip, and npm: `--global` and `--local` select where Hatchery configuration
is edited. Sandbox files remain under the `sandbox` command because their
scope is the current repository or a specific task rather than global/local.

## Summary

- Added a packaged, configurable shared workflow body without changing either
  existing rendered session prompt. Commit and no-commit modes retain their
  original introductions and task-record endings as fixed framing around that
  shared body; exact regression tests protect both complete strings.
- Global workflow prompts live at `~/.hatchery/system-prompt.md` and seed from
  the packaged default. An optional `<repo>/.hatchery/system-prompt.md`
  completely overrides the global body.
- Added `hatchery config edit system-prompt --global/--local` and
  `hatchery config reset system-prompt --global/--local`. General YAML config
  supports `hatchery config edit --global/--local`, with the previous
  positional forms retained for compatibility.
- Task launches render the effective workflow body with mode-specific framing,
  then append dynamic sandbox context. Chat sessions remain unchanged.
- Sandbox files remain under `hatchery sandbox` because they use repository or
  task scope rather than global/local configuration scope.
- Validation passed: Ruff formatting for `src`/`tests`, Ruff linting, and
  1,303 non-kubectl tests (22 skipped). The container's cryptography build still
  terminates with an illegal-instruction error in the existing kubectl
  certificate tests.
- The stacked `update-prompt` branch changes only the shared prompt wording
  and its corresponding documentation/tests.
