# seekr-hatchery

Task orchestration CLI for AI coding agents — manages isolated git worktrees with optional Docker sandboxing.

## Package management
- Use `uv` for all Python dependency management
- Install deps: `uv sync`
- Run scripts: `uv run <script>`
- Add dependencies: `uv add <package>`

## Python version
- Requires Python 3.12+ (see `.python-version`)
- Use modern Python features freely: `match/case`, structural pattern matching, `|` union types

## Linting & formatting
- Use `ruff` for linting and formatting
- Format: `uv run ruff format .`
- Lint: `uv run ruff check .`
- Fix: `uv run ruff check --fix .`

## Code style
- Full type annotations on all functions (return types included)
- Use `import seekr_hatchery.ui as ui` for all user-facing output (`ui.error()`, `ui.warn()`, `ui.info()`, `ui.success()`, etc.) — never `print()`. Reserve `logger.*()` for internal/diagnostic output only (visible at `--log-level DEBUG/INFO`).
- Error paths: call `ui.error(msg)` then `sys.exit(1)`
- Keep stdlib-only; avoid adding heavy dependencies
- **Import style**: prefer `import seekr_hatchery.foo as foo` over `from seekr_hatchery.foo import a, b, c` — use `foo.bar()` at call sites so the origin of every symbol is obvious

## Project structure
- `src/seekr_hatchery/cli.py` — main CLI entry point
- `.hatchery/tasks/` — permanent task records, tracked in git
- `.hatchery/worktrees/` — active worktrees, gitignored
- `~/.hatchery/` — host-level task database (JSON metadata + per-task auth copies)

## Dev dependencies
- `ruff` and `pytest` are dev dependencies in `[dependency-groups] dev` (uv-native dev group)
- Install with `uv sync` (included by default)

## Testing guidelines

### 1. Test contracts, not implementation details
Test the *observable contract* of a function or class: given certain inputs,
assert on return values and side effects (e.g. files written, calls made to
collaborators). Do not reach into private methods or patch internal state to
verify how something is done — only verify that it produces the right outcome.

Private (`_prefixed`) methods should not have their own tests. If a private
method's behaviour matters, it is observable through the public API.

### 2. Keep mocking to a minimum
Only mock things that cross a process boundary (filesystem, network, subprocess,
time) or that are prohibitively expensive to run in a test. Prefer using real
objects and `tmp_path` for filesystem interaction. Excessive mocking ties tests
to implementation structure and makes refactors painful.

When you do need to isolate a collaborator, prefer a simple hand-written test
double (see `conftest.py`) over mocking individual methods on real objects.

### 3. Assert full outputs, not individual properties
When a function returns a structure (list, dict, dataclass), write one assertion
on the *entire* return value rather than separate assertions on individual fields.
This catches unexpected extra output and keeps each test self-contained.

```python
# Bad — two tests, each checking one field of the same call
def test_default_schema_version(): assert model.schema_version == "1"
def test_default_agent_is_none():  assert model.default_agent is None

# Good — one test, full output
def test_defaults():
    assert UserConfigModel().model_dump() == {"schema_version": "1", "default_agent": None}
```

For functions with multiple call variants, write one test *per variant* and
assert the full output of each. Use `pytest.mark.parametrize` when the variants
share the same assertion shape.

```python
# Bad — four tests, each checking one flag across two variants
def test_native_has_permission_mode(): ...
def test_native_has_session_id(): ...
def test_docker_has_skip_permissions(): ...
def test_docker_has_settings(): ...

# Good — one test per variant, full command asserted
def test_native(): assert build_command(...) == ["claude", "--permission-mode=plan", ...]
def test_docker(): assert build_command(..., docker=True) == ["claude", "--allow-dangerously-skip-permissions", ...]
```

### 4. Mirror `src/` in `tests/`
Test files should map one-to-one with source modules: `cli.py` → `test_cli.py`,
`agent.py` → `test_agent_<backend>.py`, etc. If a test for function `foo` lives
in the wrong file, move it — don't add a cross-module dependency.

When one module has meaningfully distinct units (e.g. one backend per file in
`agent.py`), split the test file accordingly so coverage gaps are obvious at a
glance.

## Conventional commits (required)
The **MR title** must follow the Conventional Commits format — the CI validates this on every MR. Individual branch commits are not validated (MRs are squash-merged, so only the MR title lands on `main`).

Format: `<type>(<optional scope>)<!>: <description>`

Regex: `^(feat|fix|docs|chore|style|refactor|perf|test|build|ci|revert|no-bump)(\([^)]+\))?(!)?: .+`

Version bump rules (actual CI behaviour in `setup.yml`):
- `no-bump:` → **skip release entirely** (no tag, no PyPI publish)
- any type with `!` (e.g. `feat!:`) → **major** bump (x.0.0)
- `feat:` → **minor** bump (0.x.0)
- `fix:`, `perf:` → **patch** bump (0.0.x)
- everything else (`docs`, `chore`, `style`, `refactor`, `test`, `build`, `ci`, `revert`) → **patch** bump (0.0.x)

**Breaking changes (`!`) are rare** — this is an interactive CLI tool, not a library. Changing defaults, adding flags, or altering UX flow is **not** breaking. Reserve `!` for changes that would genuinely break existing automation or config files (e.g. removing a command, renaming a config key that existing files use, changing the task JSON schema in a non-backwards-compatible way). When in doubt, don't mark it breaking — discuss first.

Examples:
```
feat(cli): add --dry-run flag
fix: handle missing config file gracefully
chore: update ruff to 0.15
feat!: rename `new` command to `start`
no-bump: update CI workflow variables
```

## Key constants (do not rename without updating docs)
- `TASKS_DB_DIR` — global task metadata at `~/.hatchery/tasks/`
- `WORKTREES_SUBDIR` — per-repo worktrees at `.hatchery/worktrees/`
- `SCHEMA_VERSION` — increment when task JSON schema changes

## Documentation maintenance

There is no CHANGELOG — the git tag history with conventional commit titles
serves that purpose (`git log --oneline v0.7.1..HEAD`).

Update **README.md** whenever anything changes that affects how users install,
configure, or use the tool. Skip it for internal refactors, test-only changes,
and anything else that has no effect outside the codebase.
