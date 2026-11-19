import json
from importlib.resources import files
from pathlib import Path

import seekr_hatchery.sandbox.runtime as sandbox_runtime
import seekr_hatchery.sandbox.spec as sandbox_spec
import seekr_hatchery.sandbox.spec as spec


def test_dind_seccomp_resource() -> None:
    result = spec.build_spec(
        image="image",
        mounts=[],
        workdir="/work",
        name="task",
        hatchery_repo="repo",
        container_name=None,
        agent_cmd=["agent"],
        dind=True,
    )
    security, seccomp = result.security_opt
    path = Path(seccomp.removeprefix("seccomp="))
    expected = files("seekr_hatchery").joinpath("resources/seccomp.json").read_text()
    assert (security, path.read_text()) == ("label=disable", expected)
    assert isinstance(json.loads(path.read_text()), dict)


# ---------------------------------------------------------------------------
# find_task_file
# ---------------------------------------------------------------------------


class TestSeccompResource:
    def test_is_absolute_path(self):
        assert sandbox_spec._SECCOMP.is_absolute()

    def test_file_exists_and_is_nonempty(self):
        assert sandbox_spec._SECCOMP.exists()
        assert sandbox_spec._SECCOMP.stat().st_size > 0

    def test_is_valid_json(self):
        import json

        data = json.loads(sandbox_spec._SECCOMP.read_text())
        assert "syscalls" in data


# ---------------------------------------------------------------------------
# find_task_file
# ---------------------------------------------------------------------------


class TestDindCapMerge:
    """Verify build_spec merges user cap_add with DinD defaults."""

    _COMMON = dict(
        image="test-image:task",
        mounts=[],
        workdir="/repo/.hatchery/worktrees/task",
        hatchery_repo="/repo",
        name="task",
        agent_cmd=["codex"],
    )

    def _run(self, **kwargs) -> list[str]:
        args = {**self._COMMON, **kwargs}
        spec = sandbox_spec.build_spec(**args, container_name=None)
        return sandbox_runtime.DockerRuntime().render_run_argv(spec)

    def test_user_caps_merged(self):
        cmd = self._run(dind=True, cap_add=["NET_BIND_SERVICE"])
        cap_add_values = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--cap-add"]
        assert "NET_BIND_SERVICE" in cap_add_values

    def test_duplicates_eliminated(self):
        cmd = self._run(dind=True, cap_add=["SYS_ADMIN"])
        cap_add_values = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--cap-add"]
        assert cap_add_values.count("SYS_ADMIN") == 1

    def test_caps_ignored_when_dind_false(self):
        cmd = self._run(dind=False, cap_add=["NET_ADMIN"])
        assert "--cap-add" not in cmd
