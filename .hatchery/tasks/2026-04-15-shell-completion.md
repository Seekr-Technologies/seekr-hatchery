# Task: shell-completion

**Status**: complete
**Branch**: hatchery/shell-completion
**Created**: 2026-04-15 14:53

## Objective

Add shell completion for the CLI, specifically for commands like `resume` and `delete`.

## Summary

### Approach

Used Click 8.3's modern `ParamType` subclass API with a `shell_complete()` method — the canonical approach introduced in Click 8.0. This is preferable to the deprecated `autocompletion=` callback from Click 7.

### Key decisions

**`TaskNameType` custom ParamType** (`src/seekr_hatchery/cli.py`): A minimal `click.ParamType` subclass that calls the already-imported `git.git_root_or_cwd()` and `tasks.repo_tasks_for_current_repo()` inside `shell_complete()`. The `convert()` method passes values through unchanged (no runtime validation — commands already validate task existence via `tasks.load_task()`). All exceptions are swallowed — a crashing completion would kill the user's shell session.

**Commands updated** with `type=TASK_NAME`: `resume`, `delete`, `done`, `archive`, `exec`, `shell`, `status`, `chat` (optional arg). Skipped: `new` (creates new names) and `abort` (hidden, errors out).

**Completion includes help text**: `CompletionItem(name, help=status)` so zsh/fish users see task status alongside each name.

**`hatchery completion <shell>`** subcommand prints the one-liner users need to add to their shell config. This is the standard pattern (no subprocess — just prints the eval line). Supports bash, zsh, fish.

### Activation (for users)

```bash
# bash — add to ~/.bashrc:
eval "$(_HATCHERY_COMPLETE=bash_source hatchery)"

# zsh — add to ~/.zshrc:
eval "$(_HATCHERY_COMPLETE=zsh_source hatchery)"

# fish — add to ~/.config/fish/config.fish:
_HATCHERY_COMPLETE=fish_source hatchery | source
```

Or run `hatchery completion bash` (etc.) to see the instruction.

### Files changed

- `src/seekr_hatchery/cli.py`: Added `TaskNameType` class + `TASK_NAME` singleton, updated 8 argument declarations, added `cmd_completion` command
- `tests/test_cli.py`: Added `"completion"` to `expected_commands`, added `TestCompletion` class (7 tests)

**`hatchery self completions`** subcommand auto-installs the activation line into the user's shell rc file. Detects shell from `$SHELL`, uses `Path(sys.argv[0]).resolve()` for the binary path (works for both `uv tool install` and `uv run hatchery` in a worktree). Idempotent — skips if `_HATCHERY_COMPLETE` is already present in the rc file.

### Files changed

- `src/seekr_hatchery/cli.py`: `TaskNameType` + `TASK_NAME`, 8 argument type annotations, `cmd_completion`, `cmd_self_completions`, update-check suppressed during completion
- `tests/test_cli.py`: `"completion"` in `expected_commands`, `TestCompletion` (7 tests), `TestSelfCompletions` (5 tests)

### Gotchas

**`ui.warn()` writes to stdout**, not stderr. The `cli` group callback calls `_check_for_update()` which can trigger `ui.warn()`. During completion, the hatchery binary is run as a subprocess whose stdout is parsed by the shell — any extra stdout corrupts the completion data and produces no completions. Fixed by skipping the update check when `_HATCHERY_COMPLETE` is set.

**Full-path invocation doesn't get completion**: `compdef` registers completions for the bare command name `hatchery`. Running `/full/path/to/hatchery resume <TAB>` won't trigger it — you need `hatchery` on PATH. This is only relevant when testing from a worktree venv; production installs via `uv tool install` are always on PATH.

**Dev version triggers update warning**: A worktree install resolves as `0.0.0+dev`, which is always lower than any published PyPI version, so the update warning always fires. This is what caused the stdout corruption above.
