# Task: ls-shorthand

**Status**: complete
**Branch**: hatchery/ls-shorthand
**Created**: 2026-04-15 14:46

## Objective

Add CLI improvements:
- `ls` shorthand for the `hatcher ls` command
- `archive`, `delete`, and `done` should work with a list of task names
- Propose other QoL CLI improvements

## Summary

All changes in `src/seekr_hatchery/cli.py`.

### Changes implemented

**Hidden shorthands** (don't appear in `--help`, avoiding duplicate entries):
- `hatchery ls [--all/-a]` → delegates to `list` via `ctx.invoke`
- `hatchery st <name>` → delegates to `status` via `ctx.invoke`

**Short flag:** `-a` added to `list` and `ls` as shorthand for `--all`.

**Multi-name support for `done`, `archive`, `delete`:** Changed `@click.argument("name")` to `@click.argument("names", nargs=-1, required=True)` in all three. Each loops over the names. Archive and done retain per-task interactive prompts (uncommitted changes check is task-specific and needs individual responses). Delete uses a single batch confirmation when >1 name is provided.

**`--force/-f` for `delete`:** Added to skip the confirmation prompt, useful in scripts or when already confident. Works for both single and multi-name invocations. Implemented by adding a `confirmed: bool = False` kwarg to `_do_delete()`.

### Proposed future improvements not implemented

- **Prefix-matching for task names** — allow `hatchery done my-long` if unambiguous; would go in `tasks.load_task()`
- **Shell tab completion** — Click's built-in completion via `shellingham`/`click-completion` packages

### Key decisions

- Used `hidden=True` on `ls`/`st` so help output stays clean with no duplicate entries
- Used `ctx.invoke` for delegation — zero code duplication, single source of truth
- Kept `archive` and `done` with per-task prompts (each task's uncommitted-changes situation is independent)
- Batch delete confirmation shows all task names upfront so user can verify before confirming
- `_do_delete` gets a `confirmed` kwarg rather than a separate helper — simpler and more future-proof
