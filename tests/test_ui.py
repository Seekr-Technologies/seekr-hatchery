from pathlib import Path

import seekr_hatchery.ui as ui


def test_sandbox_banner_displays_hatchery_dir_with_aligned_values(capsys):
    ui.banner(
        "my-task",
        Path("/repo"),
        branch="hatchery/my-task",
        sandbox=True,
        features=["DinD"],
        worktree_path=Path(".hatchery/worktrees/my-task"),
        hatchery_dir=Path(".hatchery/worktrees/my-task/.hatchery"),
    )

    assert capsys.readouterr().out == (
        "╔══ sandbox ═════════════════════════════════════════╗\n"
        "║  Task:      my-task                                ║\n"
        "║  Branch:    hatchery/my-task                       ║\n"
        "║  Repo:      /repo                                  ║\n"
        "║  Worktree:  .hatchery/worktrees/my-task            ║\n"
        "║  Hatchery:  .hatchery/worktrees/my-task/.hatchery  ║\n"
        "║  Features:  DinD                                   ║\n"
        "╚════════════════════════════════════════════════════╝\n"
    )


def test_chat_banner_displays_hatchery_dir_with_aligned_values(capsys):
    ui.chat_banner(
        "chat-1",
        Path("/repo"),
        features=["DinD"],
        hatchery_dir=Path(".hatchery"),
    )

    assert capsys.readouterr().out == (
        "╔══ sandbox ════════════════════════════════════╗\n"
        "║  Chat:      chat-1                            ║\n"
        "║  Dir:       /repo                             ║\n"
        "║  Hatchery:  .hatchery                         ║\n"
        "║  Features:  DinD                              ║\n"
        "╚═══════════════════════════════════════════════╝\n"
    )


def test_native_banner_omits_hatchery_dir(capsys):
    ui.banner("my-task", Path("/repo"), sandbox=False)

    assert capsys.readouterr().out == (
        "┌── worktree ───────────────────────────────────┐\n"
        "│  Task:  my-task                               │\n"
        "│  Repo:  /repo                                 │\n"
        "└───────────────────────────────────────────────┘\n"
    )
