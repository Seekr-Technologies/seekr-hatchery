---
name: hatchery-done
description: Complete the current hatchery task. Use when the user invokes /hatchery-done, says "mark this done", "finish the task", "I'm done", or wants to wrap up and close the work session.
disable-model-invocation: true
allowed-tools: Read, Edit, Glob, Bash(git add:*), Bash(git commit:*), Bash(git status:*)
---

Finalize this hatchery task by converting the task file to ADR format and preparing for
handoff.

## Current state
- Git status: !`git status --short`

## Steps

1. **Find and read the task file** — use Glob to find `*.md` in `.hatchery/tasks/`.

2. **Assess completion** — if unfinished implementation work remains, list what's missing
   and stop. The user should finish the work then invoke `/hatchery-done` again.

3. **Convert task file to ADR format:**
   - Write `## Summary` covering: key decisions, patterns, files changed, gotchas, and
     anything a future agent should know.
   - Change `**Status**: in-progress` → `**Status**: complete`.
   - Remove the entire `## Agreed Plan` section and its contents.
   - Remove the entire `## Progress Log` section and its contents.
   - Final structure: Status/Branch/Created metadata → Objective → Context → Summary.

4. **Commit the finalized task file:**
   ```
   git add .hatchery/tasks/
   git commit -m "task(NAME): complete — add ADR summary"
   ```
   Replace NAME with the actual task name.

5. **Confirm to the user:**
   - The task file has been finalized and committed.
   - They should run `hatchery done NAME` from their host terminal to remove the worktree.
   - This Claude session can now be closed.
