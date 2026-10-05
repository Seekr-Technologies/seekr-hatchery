# Task: pi-skills

**Status**: in-progress
**Branch**: hatchery/pi-skills
**Created**: 2026-09-30 14:52

## Objective

Update the pi harness to link in (RW) ~/.pi/agent/skills

## Agreed Plan

1. Update `PiBackend.construct_mounts()` to create and bind-mount host `~/.pi/agent/skills` into the sandbox read-write, with symlink-target handling matching `extensions/`.
2. Extend `tests/test_agent_pi.py` to verify the skills directory is created and mounted correctly alongside existing Pi state.
3. Update `README.md` to document Pi skills sharing, then run focused tests plus Ruff checks.
4. Finalize this task record as an ADR and mark the task complete.

## Progress Log

- [x] Create the read-write Pi skills mount.
- [x] Add mount coverage for Pi skills.
- [x] Document the behavior and run validation (42 focused tests pass; Ruff passes on changed Python files).
- [ ] Finalize the task record.

## Summary

*(Fill in on completion — then remove Agreed Plan and Progress Log above.
Cover: key decisions made, patterns established, files changed, gotchas,
and anything a future agent working in this repo should know.)*
