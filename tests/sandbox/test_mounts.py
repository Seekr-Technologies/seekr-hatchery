from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

import seekr_hatchery.agents as agent
import seekr_hatchery.branches as branches
import seekr_hatchery.constants as constants
import seekr_hatchery.includes as includes
import seekr_hatchery.sandbox.config as config
import seekr_hatchery.sandbox.config as sandbox_config
import seekr_hatchery.sandbox.mount_types as mount
import seekr_hatchery.sandbox.mount_types as mount_types
import seekr_hatchery.sandbox.mounts as mounts
import seekr_hatchery.sandbox.mounts as sandbox_mounts
import seekr_hatchery.sandbox.runtime as sandbox_runtime
import seekr_hatchery.sessions as sessions
from seekr_hatchery.models import SessionMeta


def test_build_shell_mounts_order_and_policy(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    (home / ".gitconfig").write_text("[user]\nname = test\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    custom = tmp_path / "custom"
    custom.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    (custom / "link").symlink_to(target)
    cfg = config.SandboxConfig(
        mounts=[f"{custom}:/custom:rw", f"{custom}:/usr/unsafe:ro"],
        volumes=[config.CacheVolume(name="cache", path="/cache")],
        follow_symlinks=True,
    )
    assert mounts.build_shell_mounts(repo, cfg) == [
        mount_types.BindMount(src=repo, dst=str(repo), mode="RW"),
        mount_types.BindMount(src=home / ".gitconfig", dst="/home/hatchery/.gitconfig", mode="RO"),
        mount_types.BindMount(src=custom, dst="/custom", mode="RW"),
        mount_types.VolumeMount(name="hatchery-cache", dst="/cache", mode="RW", task_scoped=False),
        mount_types.BindMount(src=target, dst=str(target), mode="RW"),
    ]


# ---------------------------------------------------------------------------
# DockerRuntime.available()
# ---------------------------------------------------------------------------


class TestClipboardImageMount:
    def _make_backend(self):
        b = MagicMock()
        b.construct_mounts = MagicMock(return_value=[])
        return b

    def test_no_worktree_enabled_adds_identical_mount(self, tmp_path, monkeypatch):
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        monkeypatch.setattr(sandbox_mounts, "_default_home_mounts", lambda: [])

        cfg = sandbox_config.SandboxConfig(clipboard_images=True)
        mounts = sandbox_mounts.build_mounts(_no_wt_meta(cwd), self._make_backend(), session_dir, cfg)

        clip = session_dir / "clipboard"
        assert mount.BindMount(src=str(clip), dst=str(clip), mode="RW") in mounts
        # And the directory was actually created on the host.
        assert clip.is_dir()

    def test_no_worktree_disabled_omits_mount(self, tmp_path, monkeypatch):
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        monkeypatch.setattr(sandbox_mounts, "_default_home_mounts", lambda: [])

        cfg = sandbox_config.SandboxConfig(clipboard_images=False)
        mounts = sandbox_mounts.build_mounts(_no_wt_meta(cwd), self._make_backend(), session_dir, cfg)

        clip = session_dir / "clipboard"
        assert not any(str(clip) == str(m.src) for m in mounts)
        # And we did not create the directory.
        assert not clip.exists()


# ---------------------------------------------------------------------------
# DockerRuntime.available()
# ---------------------------------------------------------------------------


class TestBuildMountsFollowLinks:
    def test_symlinked_skill_target_is_mounted_at_its_own_path(self, tmp_path, monkeypatch):
        """End-to-end: a backend declaring follow_links on a directory that
        mixes a real skill with an absolute-symlinked one keeps the directory
        bound whole and gains one mount making the link's target exist."""
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        monkeypatch.setattr(sandbox_mounts, "_default_home_mounts", lambda: [])

        skills = tmp_path / "host-claude" / "skills"
        real = skills / "agent-comm"
        real.mkdir(parents=True)
        (real / "SKILL.md").write_text("# real\n")
        linked = tmp_path / "dotfiles" / "panel-review"
        linked.mkdir(parents=True)
        (linked / "SKILL.md").write_text("# linked\n")
        (skills / "panel-review").symlink_to(linked)

        dst = "/home/hatchery/.claude/skills"
        backend = MagicMock()
        backend.construct_mounts = MagicMock(
            return_value=[mount.BindMount(src=skills, dst=dst, mode="RW", follow_links=True)]
        )
        mounts = sandbox_mounts.build_mounts(_no_wt_meta(cwd), backend, tmp_path, sandbox_config.SandboxConfig())

        binds = {m.dst: m.src for m in mounts if isinstance(m, mount.BindMount)}
        # The directory is still one mount; the real skill needs nothing.
        assert binds[dst] == skills
        assert f"{dst}/agent-comm" not in binds
        assert f"{dst}/panel-review" not in binds
        # ...and the link's stored host path now exists in the container.
        assert binds[str(linked)] == linked
        assert not any(getattr(m, "follow_links", False) for m in mounts)


# ---------------------------------------------------------------------------
# DockerRuntime.available()
# ---------------------------------------------------------------------------


class TestBuildMountsDropsExpandedSystemMounts:
    def test_symlink_into_a_system_path_never_reaches_the_runtime(self, tmp_path, monkeypatch, capsys):
        """The seam, end to end.

        Expansion emits a mount at /usr/share because that is where the link
        points; validation then drops it. Asserted through build_mounts so the
        two halves stay pinned together.

        An absolute link deliberately, not the relative-offset case: that one
        only lands on a system path when the container directory is shallower
        than the host one, which under a pytest tmp_path means the *host*
        target has to sit outside tmp_path. Not worth writing outside the
        fixture for — ``test_mount_links.py`` covers that arithmetic at the
        unit level, where the target stays inside the pytest root.
        """
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        monkeypatch.setattr(sandbox_mounts, "_default_home_mounts", lambda: [])

        skills = tmp_path / "host-claude" / "skills"
        skills.mkdir(parents=True)
        dst = "/home/hatchery/.claude/skills"
        (skills / "sys").symlink_to("/usr/share", target_is_directory=True)

        backend = MagicMock()
        backend.construct_mounts = MagicMock(
            return_value=[mount.BindMount(src=skills, dst=dst, mode="RW", follow_links=True)]
        )
        mounts = sandbox_mounts.build_mounts(_no_wt_meta(cwd), backend, tmp_path, sandbox_config.SandboxConfig())

        assert "/usr/share" not in [m.dst for m in mounts]
        assert "shadow the sandbox" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# DockerRuntime.available()
# ---------------------------------------------------------------------------


class TestValidateMounts:
    """The one place mount safety is decided, for every source of mounts.

    A dangerous mount can be asked for directly (``docker.yaml mounts:``) or
    arrive from a resolved symlink, so the check runs over the finished list
    rather than inside each producer.
    """

    def test_ordinary_mounts_pass_through_unchanged(self, tmp_path):
        mounts = [
            mount.BindMount(src=tmp_path, dst="/home/hatchery/x", mode="RW"),
            mount.VolumeMount(name="v", dst="/home/hatchery/v"),
            mount.TmpfsMount(dst="/tmp/scratch"),
        ]
        assert _backend_mounts(mounts) == mounts

    @pytest.mark.parametrize("dst", ["/usr", "/usr/lib/foo", "/etc/ssl", "/proc", "/dev/shm"])
    def test_system_destinations_dropped(self, tmp_path, dst, capsys):
        keep = mount.BindMount(src=tmp_path, dst="/home/hatchery/keep", mode="RW")
        blocked = mount.BindMount(src=tmp_path, dst=dst, mode="RW")

        assert _backend_mounts([keep, blocked]) == [keep]
        # Dropped, but never silently.
        out = capsys.readouterr().out
        assert dst.split("/")[1] in out
        assert str(tmp_path) in out

    def test_subpaths_of_unblocked_roots_are_kept(self, tmp_path):
        # /tmp, /var, /home and /opt are deliberately not blocked.
        mounts = [mount.BindMount(src=tmp_path, dst=d, mode="RW") for d in ("/tmp/x", "/var/x", "/opt/x", "/home/x")]
        assert _backend_mounts(mounts) == mounts

    def test_non_bind_mounts_are_never_dropped(self):
        # Volume/tmpfs mounts have no host source to judge; /dev/shm as a
        # tmpfs destination is the container's business, not ours.
        mounts = [mount.TmpfsMount(dst="/dev/shm"), mount.VolumeMount(name="v", dst="/usr/local")]
        assert _backend_mounts(mounts) == mounts

    def test_macos_unshared_source_warns_but_is_kept(self, tmp_path, monkeypatch, capsys):
        """A podman machine shares only /Users and /private/tmp.

        Anything else binds as an empty directory with no error at all, so the
        warning is the only signal the user ever gets.
        """
        monkeypatch.setattr(sandbox_runtime.sys, "platform", "darwin")
        m = mount.BindMount(src=tmp_path, dst="/home/hatchery/x", mode="RW")

        assert _backend_mounts([m]) == [m]
        assert "may appear empty" in capsys.readouterr().out

    def test_macos_warns_for_a_single_file_bind(self, tmp_path, monkeypatch, capsys):
        """The gap this closes: a symlinked config *file*.

        It needs no follow_links — `-v` resolves the source — but it can still
        resolve somewhere the VM cannot see, and nothing used to say so.
        """
        target = tmp_path / "dotfiles" / "AGENTS.md"
        target.parent.mkdir()
        target.write_text("# global\n")
        link = tmp_path / "home" / ".codex" / "AGENTS.md"
        link.parent.mkdir(parents=True)
        link.symlink_to(target)
        monkeypatch.setattr(sandbox_runtime.sys, "platform", "darwin")

        m = mount.BindMount(src=link, dst="/home/hatchery/.codex/AGENTS.md", mode="RW")
        assert _backend_mounts([m]) == [m]
        out = capsys.readouterr().out
        assert str(target) in out  # names the resolved target, not just the link

    def test_macos_shared_source_is_silent(self, monkeypatch, capsys):
        monkeypatch.setattr(sandbox_runtime.sys, "platform", "darwin")
        m = mount.BindMount(src=Path("/Users/someone/x"), dst="/home/hatchery/x", mode="RW")

        assert _backend_mounts([m]) == [m]
        assert capsys.readouterr().out == ""

    def test_non_darwin_does_not_warn(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(sandbox_runtime.sys, "platform", "linux")
        m = mount.BindMount(src=tmp_path, dst="/home/hatchery/x", mode="RW")

        assert _backend_mounts([m]) == [m]
        assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# DockerRuntime.available()
# ---------------------------------------------------------------------------


class TestBuildMountsNoCommit:
    def _make_backend(self):
        b = MagicMock()
        b.construct_mounts = MagicMock(return_value=[])
        return b

    def test_no_commit_task_has_ro_store_and_rw_task_file(self, tmp_path, monkeypatch):
        """No-commit task: RO store mount + RW own-task-file mount."""
        monkeypatch.setattr(sandbox_mounts, "_default_home_mounts", lambda: [])
        monkeypatch.setattr(constants, "HATCHERY_DIR", tmp_path)

        monkeypatch.setattr(sessions, "_TASKS_DB_DIR", tmp_path / "tasks")

        meta = SessionMeta(
            name="t",
            repo=str(tmp_path),
            worktree=str(tmp_path / "wt"),
            no_commit=True,
            type="task",
        )
        # Create the task file (in its per-task subdirectory) so meta.task_file resolves
        hdir = meta.hatchery_dir
        (hdir / "tasks" / "2026-01-01-t").mkdir(parents=True)
        task_file = hdir / "tasks" / "2026-01-01-t" / "task.md"
        task_file.write_text("# task\n")

        cfg = sandbox_config.SandboxConfig()
        mounts = sandbox_mounts.build_mounts(meta, self._make_backend(), tmp_path / "sd", cfg)

        # 1. RO mount of the whole hatchery_dir
        expected_ro = mount.BindMount(src=str(hdir), dst=str(hdir), mode="RO")
        assert expected_ro in mounts

        # 2. RW mount of the task's subdirectory (not the file itself — a
        #    directory-level mount is required for atomic tmpfile+rename saves)
        expected_rw = mount.BindMount(src=str(task_file.parent), dst=str(task_file.parent), mode="RW")
        assert expected_rw in mounts

        # 3. No other RW mount under hdir (no sibling task or Dockerfile RW)
        rw_under_hdir = [
            m for m in mounts if isinstance(m, mount.BindMount) and m.mode == "RW" and str(hdir) in str(m.src)
        ]
        assert len(rw_under_hdir) == 1
        assert str(rw_under_hdir[0].src) == str(task_file.parent)

    def test_commit_mode_no_store_mount(self, tmp_path, monkeypatch):
        """Commit mode: no hatchery_dir mount at all."""
        monkeypatch.setattr(sandbox_mounts, "_default_home_mounts", lambda: [])
        meta = SessionMeta(
            name="t",
            repo=str(tmp_path),
            worktree=str(tmp_path / "wt"),
            no_commit=False,
            type="task",
        )
        cfg = sandbox_config.SandboxConfig()
        mounts = sandbox_mounts.build_mounts(meta, self._make_backend(), tmp_path / "sd", cfg)
        for m in mounts:
            if hasattr(m, "src") and "repos" in str(m.src):
                assert False, "hatchery_dir mount should not be present in commit mode"

    def test_no_commit_chat_no_store_mount(self, tmp_path, monkeypatch):
        """Chat type: no store mount even in no-commit mode."""
        monkeypatch.setattr(sandbox_mounts, "_default_home_mounts", lambda: [])
        monkeypatch.setattr(constants, "HATCHERY_DIR", tmp_path)

        monkeypatch.setattr(sessions, "_TASKS_DB_DIR", tmp_path / "tasks")

        meta = SessionMeta(
            name="t",
            repo=str(tmp_path),
            worktree=str(tmp_path),
            no_commit=True,
            type="chat",
            no_worktree=True,
        )
        cfg = sandbox_config.SandboxConfig()
        mounts = sandbox_mounts.build_mounts(meta, self._make_backend(), tmp_path / "sd", cfg)
        for m in mounts:
            if hasattr(m, "src") and "repos" in str(m.src):
                assert False, "hatchery_dir mount should not be present for chat type"

    def test_no_commit_task_file_missing_ro_only(self, tmp_path, monkeypatch):
        """If find_task_file returns None, only the RO store mount is present."""
        monkeypatch.setattr(sandbox_mounts, "_default_home_mounts", lambda: [])
        monkeypatch.setattr(constants, "HATCHERY_DIR", tmp_path)

        monkeypatch.setattr(sessions, "_TASKS_DB_DIR", tmp_path / "tasks")

        meta = SessionMeta(
            name="t",
            repo=str(tmp_path),
            worktree=str(tmp_path / "wt"),
            no_commit=True,
            type="task",
        )
        # No task file created — meta.task_file will be None
        hdir = meta.hatchery_dir
        hdir.mkdir(parents=True, exist_ok=True)

        cfg = sandbox_config.SandboxConfig()
        mounts = sandbox_mounts.build_mounts(meta, self._make_backend(), tmp_path / "sd", cfg)

        # RO mount present
        expected_ro = mount.BindMount(src=str(hdir), dst=str(hdir), mode="RO")
        assert expected_ro in mounts

        # No RW mount under hdir
        rw_under_hdir = [
            m for m in mounts if isinstance(m, mount.BindMount) and m.mode == "RW" and str(hdir) in str(m.src)
        ]
        assert len(rw_under_hdir) == 0


# ---------------------------------------------------------------------------
# DockerRuntime.available()
# ---------------------------------------------------------------------------


class TestBuildMountsBranchPrefix:
    def test_mounts_configured_branch_namespace(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sandbox_mounts, "_default_home_mounts", lambda: [])
        repo = tmp_path / "repo"
        worktree = repo / ".hatchery" / "worktrees" / "task"
        branch_refs = repo / ".git" / "refs" / "heads" / "team" / "agents"
        branch_refs.mkdir(parents=True)
        worktree.mkdir(parents=True)
        backend = MagicMock()
        backend.construct_mounts.return_value = []
        meta = SessionMeta(
            name="task",
            repo=str(repo),
            worktree=str(worktree),
            branch="team/agents/task",
            branch_prefix="team/agents/",
        )

        mounts = sandbox_mounts.build_mounts(meta, backend, tmp_path / "session", sandbox_config.SandboxConfig())

        assert (
            mount.BindMount(
                src=str(branch_refs),
                dst=f"{repo}/.git/refs/heads/team/agents",
                mode="RW",
            )
            in mounts
        )
        assert (
            mount.BindMount(
                src=str(repo / ".git" / "refs" / "heads" / "hatchery"),
                dst=f"{repo}/.git/refs/heads/hatchery",
                mode="RW",
            )
            not in mounts
        )


# ---------------------------------------------------------------------------
# DockerRuntime.available()
# ---------------------------------------------------------------------------


class TestBuildMountsIncludesVolumes:
    def _make_backend(self):
        b = MagicMock()
        b.construct_mounts = MagicMock(return_value=[])
        return b

    def test_no_worktree_appends_volume_mount(self, tmp_path, monkeypatch):
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        monkeypatch.setattr(sandbox_mounts, "_default_home_mounts", lambda: [])

        cfg = sandbox_config.SandboxConfig(volumes=[{"name": "uv-cache", "path": "/home/hatchery/.cache/uv"}])
        mounts = sandbox_mounts.build_mounts(_no_wt_meta(cwd), self._make_backend(), tmp_path, cfg)

        expected = mount.VolumeMount(
            name="hatchery-uv-cache", dst="/home/hatchery/.cache/uv", mode="RW", task_scoped=False
        )
        assert expected in mounts


# ---------------------------------------------------------------------------
# DockerRuntime.available()
# ---------------------------------------------------------------------------


class TestDefaultHomeMounts:
    def test_default_home_mounts(self, tmp_path, monkeypatch):
        # Canary: assert the exact set of default home mounts so any
        # accidental change to the defaults shows up loudly in tests.
        home = tmp_path / "home"
        (home / ".cache" / "uv").mkdir(parents=True)
        (home / ".gitconfig").write_text("[user]\n")
        monkeypatch.setattr(sandbox_runtime.Path, "home", lambda: home)

        assert sandbox_mounts.build_shell_mounts(Path("/Users/workspace"), sandbox_config.SandboxConfig())[1:] == [
            mount.BindMount(src=str(home / ".gitconfig"), dst=f"{agent.CONTAINER_HOME}/.gitconfig", mode="RO"),
        ]


# ---------------------------------------------------------------------------
# DockerRuntime.available()
# ---------------------------------------------------------------------------


class TestConstructVolumeMounts:
    def test_empty(self):
        assert _shell_config_mounts(sandbox_config.SandboxConfig()) == []

    def test_prefixes_name_and_emits_volume_mount(self):
        cfg = sandbox_config.SandboxConfig(
            volumes=[
                {"name": "uv-cache", "path": "/home/hatchery/.cache/uv"},
                {"name": "pip-cache", "path": "/home/hatchery/.cache/pip"},
            ]
        )
        assert _shell_config_mounts(cfg) == [
            mount.VolumeMount(name="hatchery-uv-cache", dst="/home/hatchery/.cache/uv", mode="RW", task_scoped=False),
            mount.VolumeMount(name="hatchery-pip-cache", dst="/home/hatchery/.cache/pip", mode="RW", task_scoped=False),
        ]


def _task_branch(name: str = "my-task", prefix: str = branches.DEFAULT_BRANCH_PREFIX) -> branches.BranchName:
    return branches.BranchPrefix(prefix).task_branch(name)


def _no_wt_meta(cwd):
    """Synthetic SessionMeta for no-worktree mount tests."""
    return SessionMeta(name="-", repo=str(cwd), worktree=str(cwd), no_worktree=True)


# ---------------------------------------------------------------------------
# DockerRuntime.available()
# ---------------------------------------------------------------------------


class TestDockerMountsIncludes:
    def _entry(self, path, mode="worktree"):
        from seekr_hatchery.includes import IncludeEntry

        return IncludeEntry(path=path, mode=mode)

    def test_plain_dir_gets_rw_mount(self, tmp_path):
        """A plain (non-git) directory in worktree mode is mounted rw at its host path."""
        plain = tmp_path / "shared-data"
        plain.mkdir()
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        mounts = _included_task_mounts(
            [self._entry(plain)], "my-task", session_dir, no_worktree=False, branch=_task_branch()
        )

        assert mount.BindMount(src=str(plain), dst=str(plain), mode="RW") in mounts

    def test_git_repo_without_worktree_gets_rw_mount(self, tmp_path):
        """A git repo in worktree mode with no worktree for the task falls back to rw mount."""
        repo = tmp_path / "repo-b"
        repo.mkdir()
        (repo / ".git").mkdir()
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        mounts = _included_task_mounts(
            [self._entry(repo)], "my-task", session_dir, no_worktree=False, branch=_task_branch()
        )

        assert mount.BindMount(src=str(repo), dst=str(repo), mode="RW") in mounts
        assert not any("git_ptr" in str(m.src or "") for m in mounts)

    def test_git_repo_with_worktree_gets_layered_mounts(self, tmp_path):
        """A git repo in worktree mode with a task worktree gets layered mounts at host paths."""

        repo = tmp_path / "repo-b"
        repo.mkdir()
        git_dir = repo / ".git"
        git_dir.mkdir()
        (git_dir / "objects").mkdir()
        worktree = repo / constants.WORKTREES_SUBDIR / "my-task"
        worktree.mkdir(parents=True)
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        mounts = _included_task_mounts(
            [self._entry(repo)], "my-task", session_dir, no_worktree=False, branch=_task_branch()
        )

        assert mount.BindMount(src=str(repo), dst=str(repo), mode="RO") in mounts
        assert mount.BindMount(src=str(git_dir), dst=f"{repo}/.git", mode="RW") in mounts
        assert mount.BindMount(src=str(git_dir / "objects"), dst=f"{repo}/.git/objects", mode="RW") in mounts
        assert mount.BindMount(src=str(worktree), dst=str(worktree), mode="RW") in mounts
        # No .git pointer rewrite — under host-path mirroring, the worktree's
        # existing .git file already resolves correctly inside the container.
        assert not any("git_ptr" in str(m.src or "") for m in mounts)
        assert mount.BindMount(src=str(repo), dst=str(repo), mode="RW") not in mounts

    def test_git_repo_uses_custom_branch_ref_namespace(self, tmp_path):
        repo = tmp_path / "repo-b"
        git_dir = repo / ".git"
        branch_refs = git_dir / "refs" / "heads" / "agents"
        branch_refs.mkdir(parents=True)
        (git_dir / "objects").mkdir()
        (repo / constants.WORKTREES_SUBDIR / "my-task").mkdir(parents=True)

        mounts = _included_task_mounts(
            [self._entry(repo)],
            "my-task",
            tmp_path / "session",
            no_worktree=False,
            branch=_task_branch(prefix="agents/"),
        )

        assert mount.BindMount(src=str(branch_refs), dst=f"{repo}/.git/refs/heads/agents", mode="RW") in mounts

    def test_no_worktree_skips_layered_mounts(self, tmp_path):
        """In no-worktree mode, worktree-mode git repos get a simple rw mount."""
        repo = tmp_path / "repo-b"
        repo.mkdir()
        (repo / ".git").mkdir()

        worktree = repo / constants.WORKTREES_SUBDIR / "my-task"
        worktree.mkdir(parents=True)
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        mounts = _included_task_mounts(
            [self._entry(repo)], "my-task", session_dir, no_worktree=True, branch=_task_branch()
        )

        assert mount.BindMount(src=str(repo), dst=str(repo), mode="RW") in mounts
        assert not any("git_ptr" in str(m.src or "") for m in mounts)

    def test_empty_list_returns_empty(self, tmp_path):
        mounts = _included_task_mounts([], "task", tmp_path, no_worktree=False, branch=_task_branch("task"))
        assert mounts == []

    # ── reference mode tests ─────────────────────────────────────────────────

    def test_reference_rw_plain_dir(self, tmp_path):
        """mode='rw' gives a simple rw mount, no worktree logic."""
        plain = tmp_path / "shared-data"
        plain.mkdir()
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        mounts = _included_task_mounts(
            [self._entry(plain, mode="rw")],
            "my-task",
            session_dir,
            no_worktree=False,
            branch=_task_branch(),
        )

        assert mount.BindMount(src=str(plain), dst=str(plain), mode="RW") in mounts

    def test_reference_ro_plain_dir(self, tmp_path):
        """mode='ro' gives a simple ro mount."""
        plain = tmp_path / "docs"
        plain.mkdir()
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        mounts = _included_task_mounts(
            [self._entry(plain, mode="ro")],
            "my-task",
            session_dir,
            no_worktree=False,
            branch=_task_branch(),
        )

        assert mount.BindMount(src=str(plain), dst=str(plain), mode="RO") in mounts
        assert mount.BindMount(src=str(plain), dst=str(plain), mode="RW") not in mounts

    def test_reference_mode_git_repo_no_layered_mounts(self, tmp_path):
        """mode='ro' on a git repo with a worktree still just does a simple ro mount."""

        repo = tmp_path / "repo-b"
        repo.mkdir()
        git_dir = repo / ".git"
        git_dir.mkdir()
        (git_dir / "objects").mkdir()
        # Create a worktree — it should be ignored in reference mode
        worktree = repo / constants.WORKTREES_SUBDIR / "my-task"
        worktree.mkdir(parents=True)
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        mounts = _included_task_mounts(
            [self._entry(repo, mode="ro")],
            "my-task",
            session_dir,
            no_worktree=False,
            branch=_task_branch(),
        )

        assert mount.BindMount(src=str(repo), dst=str(repo), mode="RO") in mounts
        # No layered mounts
        assert mount.BindMount(src=str(repo), dst=str(repo), mode="RW") not in mounts
        assert not any("git_ptr" in str(m.src or "") for m in mounts)
        assert not any("worktrees" in str(m.dst or "") for m in mounts)

    def test_reference_rw_git_repo_no_layered_mounts(self, tmp_path):
        """mode='rw' on a git repo with a worktree still just does a simple rw reference mount."""

        repo = tmp_path / "repo-c"
        repo.mkdir()
        (repo / ".git").mkdir()
        worktree = repo / constants.WORKTREES_SUBDIR / "my-task"
        worktree.mkdir(parents=True)
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        mounts = _included_task_mounts(
            [self._entry(repo, mode="rw")],
            "my-task",
            session_dir,
            no_worktree=False,
            branch=_task_branch(),
        )

        assert mount.BindMount(src=str(repo), dst=str(repo), mode="RW") in mounts
        assert not any("git_ptr" in str(m.src or "") for m in mounts)
        assert not any("worktrees" in str(m.dst or "") for m in mounts)

    def test_mixed_modes(self, tmp_path):
        """Mixed worktree and reference entries produce correct mounts each."""

        wt_repo = tmp_path / "wt-repo"
        wt_repo.mkdir()
        (wt_repo / ".git").mkdir()
        ro_dir = tmp_path / "docs"
        ro_dir.mkdir()
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        from seekr_hatchery.includes import IncludeEntry

        entries = [
            IncludeEntry(path=wt_repo, mode="worktree"),
            IncludeEntry(path=ro_dir, mode="ro"),
        ]
        mounts = _included_task_mounts(entries, "my-task", session_dir, no_worktree=False, branch=_task_branch())

        # worktree entry without an actual worktree → rw fallback
        assert mount.BindMount(src=str(wt_repo), dst=str(wt_repo), mode="RW") in mounts
        # ro reference entry
        assert mount.BindMount(src=str(ro_dir), dst=str(ro_dir), mode="RO") in mounts


# ---------------------------------------------------------------------------
# write_task_file
# ---------------------------------------------------------------------------


class TestConstructDockerMounts:
    def test_returns_empty_for_empty_config(self):
        config = sandbox_config.SandboxConfig()
        assert _shell_config_mounts(config) == []

    def test_resolves_existing_host_path(self, tmp_path):
        host_dir = tmp_path / "mydir"
        host_dir.mkdir()
        config = sandbox_config.SandboxConfig(mounts=[f"{host_dir}:/container:ro"])
        result = _shell_config_mounts(config)
        assert result == [mount.BindMount(src=str(host_dir), dst="/container", mode="RO")]

    def test_skips_nonexistent_host_path(self):
        config = sandbox_config.SandboxConfig(mounts=["/no/such/path:/container:ro"])
        assert _shell_config_mounts(config) == []

    def test_defaults_mode_to_ro(self, tmp_path):
        host_dir = tmp_path / "mydir"
        host_dir.mkdir()
        config = sandbox_config.SandboxConfig(mounts=[f"{host_dir}:/container"])
        result = _shell_config_mounts(config)
        assert result == [mount.BindMount(src=str(host_dir), dst="/container", mode="RO")]

    def test_expands_tilde_in_host_path(self, home):
        (home / "mydir").mkdir()
        config = sandbox_config.SandboxConfig(mounts=["~/mydir:/container:ro"])
        result = _shell_config_mounts(config)
        assert result == [mount.BindMount(src=f"{home}/mydir", dst="/container", mode="RO")]

    def test_rw_mode_preserved(self, tmp_path):
        host_dir = tmp_path / "mydir"
        host_dir.mkdir()
        config = sandbox_config.SandboxConfig(mounts=[f"{host_dir}:/container:rw"])
        result = _shell_config_mounts(config)
        assert result == [mount.BindMount(src=str(host_dir), dst="/container", mode="RW")]

    def test_follow_symlinks_off_declares_no_follow_links(self, tmp_path):
        host_dir = tmp_path / "mydir"
        host_dir.mkdir()
        config = sandbox_config.SandboxConfig(mounts=[f"{host_dir}:/container"])
        assert _shell_config_mounts(config)[0].follow_links is False

    def test_follow_symlinks_applies_to_user_mounts(self, tmp_path):
        """The global flag covers extra mounts, not just the worktree.

        They are all directories the user asked for, so a symlink inside one
        should resolve for the same reason it does inside the worktree.
        """
        host_dir = tmp_path / "mydir"
        host_dir.mkdir()
        config = sandbox_config.SandboxConfig(mounts=[f"{host_dir}:/container:rw"], follow_symlinks=True)
        assert _shell_config_mounts(config) == [
            mount.BindMount(src=str(host_dir), dst="/container", mode="RW", follow_links=False),
        ]

    def test_dict_form_rejected(self, tmp_path):
        # Only the string form exists; follow_links comes from the global flag.
        with pytest.raises(ValidationError):
            sandbox_config.SandboxConfig(mounts=[{"host": str(tmp_path), "container": "/c"}])

    def test_non_string_entry_rejected(self):
        with pytest.raises(ValidationError):
            sandbox_config.SandboxConfig(mounts=[42])


def _shell_config_mounts(config: sandbox_config.SandboxConfig) -> list[mount.Mount]:
    repo = Path("/Users/workspace")
    baseline = sandbox_mounts.build_shell_mounts(repo, sandbox_config.SandboxConfig())
    return [m for m in sandbox_mounts.build_shell_mounts(repo, config) if m not in baseline]


def _backend_mounts(requested: list[mount.Mount]) -> list[mount.Mount]:
    backend = MagicMock()
    backend.construct_mounts.return_value = requested
    meta = _no_wt_meta(Path("/Users/workspace"))
    baseline = sandbox_mounts.build_shell_mounts(Path("/Users/workspace"), sandbox_config.SandboxConfig())
    return [
        m
        for m in sandbox_mounts.build_mounts(
            meta, backend, Path("/session"), sandbox_config.SandboxConfig(clipboard_images=False)
        )
        if m not in baseline
    ]


def _included_task_mounts(
    entries: list[includes.IncludeEntry], name: str, session_dir: Path, no_worktree: bool, branch: branches.BranchName
) -> list[mount.Mount]:
    backend = MagicMock()
    backend.construct_mounts.return_value = []
    meta = SessionMeta(
        name=name,
        repo=str(session_dir),
        worktree=str(session_dir),
        no_worktree=no_worktree,
        branch_prefix=str(branch).removesuffix(name),
    )
    config = sandbox_config.SandboxConfig(clipboard_images=False)
    baseline = sandbox_mounts.build_mounts(meta, backend, session_dir, config)
    return [
        m
        for m in sandbox_mounts.build_mounts(meta, backend, session_dir, config, include_entries=entries)
        if m not in baseline
    ]
