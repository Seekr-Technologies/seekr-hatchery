"""System-prompt storage, precedence, and editing contracts."""

from unittest.mock import patch

import seekr_hatchery.system_prompt as system_prompt


class TestPromptStorage:
    def test_seed_creates_default_without_overwriting_existing_file(self, tmp_path):
        path = tmp_path / "nested" / "system-prompt.md"

        assert system_prompt.seed(path) is True
        default = path.read_text()
        path.write_text("custom\n")

        assert system_prompt.seed(path) is False
        assert (default, path.read_text()) == (system_prompt.default_text(), "custom\n")

    def test_load_seeds_global_prompt_when_no_local_override_exists(self, tmp_path, home):
        repo = tmp_path / "repo"
        repo.mkdir()

        result = system_prompt.load(repo)

        assert result == system_prompt.default_text()
        assert system_prompt.global_path().read_text() == result

    def test_load_prefers_local_override(self, tmp_path, home):
        repo = tmp_path / "repo"
        local = system_prompt.local_path(repo)
        local.parent.mkdir(parents=True)
        local.write_text("local instructions\n")

        result = system_prompt.load(repo)

        assert result == "local instructions\n"
        assert system_prompt.global_path().read_text() == system_prompt.default_text()

    def test_edit_missing_local_prompt_starts_from_effective_global(self, tmp_path, home):
        repo = tmp_path / "repo"
        repo.mkdir()
        system_prompt.global_path().parent.mkdir(parents=True)
        system_prompt.global_path().write_text("team default\n")

        with patch("seekr_hatchery.system_prompt.open_for_editing") as editor:
            result = system_prompt.edit(repo, global_=False)

        local = system_prompt.local_path(repo)
        assert (result, local.read_text()) == (local, "team default\n")
        editor.assert_called_once_with(local)

    def test_reset_overwrites_custom_prompt_with_packaged_default(self, tmp_path):
        path = tmp_path / "system-prompt.md"
        path.write_text("custom\n")

        system_prompt.reset(path)

        assert path.read_text() == system_prompt.default_text()


def test_render_commit_prompt_preserves_original_wording(tmp_path, home):
    repo = tmp_path / "repo"

    assert (
        system_prompt.render(repo, no_commit=False)
        == 'You are working on a task. The task file is at `.hatchery/tasks/<date>-<name>/task.md`\n(the exact path is in your session prompt). This single file serves as brief,\nplan, progress log, and final notes — update it in place as you work.\n\nYour workflow:\n\n1. PLAN FIRST: Read the task file, ask any clarifying questions, then propose\n   a concrete numbered implementation plan. Do not write any code until the\n   user approves the plan.\n\n2. ON APPROVAL: Update the "Agreed Plan" section of the task file with the\n   final plan, then proceed to implement it step by step.\n\n3. WHILE EXECUTING: After each step, tick the checkbox in the Progress Log\n   and make a descriptive git commit.\n\n4. IF BLOCKED: If you hit something that would materially change the plan,\n   stop and discuss before proceeding.\n\n5. ON COMPLETION: Mark Status as "complete". Add a "## Summary" section\n   covering key decisions, patterns established, gotchas, and anything a\n   future agent should know. Then remove the "## Agreed Plan" and\n   "## Progress Log" sections — they are working scaffolding, not permanent\n   record. The final file should read as a clean ADR:\n   Status/Branch/Created → Objective → Context → Summary.\n   This file will be merged into main as the permanent record of this task.\n'
    )


def test_render_no_commit_prompt_preserves_original_wording(tmp_path, home):
    repo = tmp_path / "repo"

    assert (
        system_prompt.render(repo, no_commit=True)
        == 'You are working on a task. The task file is at the path given in your session\nprompt. This single file serves as brief, plan, progress log, and final notes —\nupdate it in place as you work.\n\nYour workflow:\n\n1. PLAN FIRST: Read the task file, ask any clarifying questions, then propose\n   a concrete numbered implementation plan. Do not write any code until the\n   user approves the plan.\n\n2. ON APPROVAL: Update the "Agreed Plan" section of the task file with the\n   final plan, then proceed to implement it step by step.\n\n3. WHILE EXECUTING: After each step, tick the checkbox in the Progress Log\n   and make a descriptive git commit.\n\n4. IF BLOCKED: If you hit something that would materially change the plan,\n   stop and discuss before proceeding.\n\n5. ON COMPLETION: Mark Status as "complete". Add a "## Summary" section\n   covering key decisions, patterns established, gotchas, and anything a\n   future agent should know. Then remove the "## Agreed Plan" and\n   "## Progress Log" sections — they are working scaffolding, not permanent\n   record. The final file should read as a clean ADR:\n   Status/Branch/Created → Objective → Context → Summary.\n   This file is your local hatchery record (stored outside the repo, not\n   committed) — future sessions can read sibling records in the same directory.\n'
    )
