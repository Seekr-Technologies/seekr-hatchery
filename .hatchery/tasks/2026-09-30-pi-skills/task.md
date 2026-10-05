# Task: pi-skills

**Status**: complete
**Branch**: hatchery/pi-skills
**Created**: 2026-09-30 14:52

## Objective

Update the pi harness to link in (RW) ~/.pi/agent/skills

## Context

Pi sandbox sessions already shared host-global extensions read-write, but global
skills remained trapped in the per-task agent volume. This prevented skills
installed or authored under `~/.pi/agent/skills` from being consistently
available on the host and across sandbox tasks.

## Summary

`PiBackend.construct_mounts()` now treats `skills/` like `extensions/`: it
creates the host directory when absent, bind-mounts it read-write at the same
path in the container, and enables symlink-target mounts for dotfiles-managed
skills. Pi's authentication files remain isolated in the per-task volume.

Mount tests cover both empty-host setup and fully populated host configuration,
including mount ordering, access mode, and symlink handling. The README now
documents shared Pi extensions and skills.

Validation completed with all 42 tests in `tests/test_agent_pi.py` passing and
Ruff lint/format checks passing for the changed Python files. The full suite was
also attempted, but the sandbox Python process exited on an illegal instruction
while importing `cryptography` in the unrelated kubectl sidecar tests.
