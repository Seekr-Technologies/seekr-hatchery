# Review: hatchery/include-reference

**Base**: origin/main
**Date**: 2026-05-08

## Staged diff

```
.hatchery/tasks/2026-05-05-include-reference.md   |  80 ++++++++++
 src/seekr_hatchery/cli.py                         | 155 +++++++++++++++---
 src/seekr_hatchery/docker.py                      | 103 ++++++++----
 src/seekr_hatchery/git.py                         |  35 +++--
 src/seekr_hatchery/resources/docker.yaml.template |  23 ++-
 src/seekr_hatchery/tasks.py                       |  67 ++++++--
 tests/test_cli.py                                 |  85 +++++-----
 tests/test_docker.py                              | 183 +++++++++++++++++++---
 tests/test_git.py                                 |  48 +++++-
 tests/test_pure.py                                |  91 ++++++++++-
 10 files changed, 722 insertions(+), 148 deletions(-)
```

---

## Instructions

You are responding to a code review. Treat this like a PR review response.

**Before writing any code:**
1. Read all comments.
2. For each comment, decide: implement directly, or raise for discussion.
3. Present your plan and any questions or pushbacks to the user.
4. Wait for agreement, then implement.

**Rules:**
- You MUST address every comment — none can be skipped.
- For clear, small instructions: implement directly if you agree.
- For questions or ambiguous suggestions (e.g. "Should we do X?"): surface them in step 3, do not assume intent.
- Push back on suggestions you think are wrong — explain your reasoning before declining.
- Do not make changes outside the scope of the review. Necessary side-effects are fine (e.g. updating imports after a rename).
- Preserve existing tests unless they are no longer relevant due to a change you are making. Removing a test MUST be discussed first.

## Comments

### src/seekr_hatchery/cli.py:862
```diff
-         "Git repos get a hatchery/<name> worktree for branch isolation. "
+         "Git repos get a hatchery/<name> worktree for branch isolation (read-write). "
+         "Repeatable; merged with docker.yaml 'include:' list."
+     ),
+ )
+>@click.option(
+>    "--include-rw",
+>    "include_rw",
+     multiple=True,
+     type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
+     metavar="PATH",
+     help=(
+         "Mount an additional directory read-write inside the container at /includes/<basename>/. "
```
Should we consider a way to edit this on resume? Or would the cannonical way to do this be to edit the docker.yaml in the task worktree?

### src/seekr_hatchery/docker.py:169
```diff
                  raise ValueError(f'mounts[{i}]: invalid mode {parts[2]!r} in {entry!r} — must be "ro" or "rw"')
          return v
  
+     @field_validator("include", mode="before")
+>    @classmethod
+     def validate_include(cls, v: list | None) -> list:
+         if v is None:
+             return []
+         valid_modes = {"worktree", "rw", "ro"}
+         for i, entry in enumerate(v):
```
This is silly. Don't write your own validator, use a config `InclueItem` which has fields, where RO/RW are `Literal['RW','RO']`, etc.

Don't reinvent the wheel

### src/seekr_hatchery/tasks.py:100
```diff
  
+ INCLUDE_MODES = {"worktree", "rw", "ro"}
+ 
+ 
+ @dataclass
+>class IncludeEntry:
+     """A single --include path together with its mount mode.
+ 
+     mode is one of:
+       "worktree" — rw with branch isolation (creates a hatchery/<name> worktree)
+       "rw"       — reference mount, read-write, no worktree
```
Does this belong here? This file is sprawling. Make a new module if you need it

