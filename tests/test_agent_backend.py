"""Unit tests for the shared AgentBackend abstractions."""

import pytest

import seekr_hatchery.agents as agent


class TestMount:
    def test_default_mode_is_rw(self):
        m = agent.Mount(src="/host/x", dst="/cont/x")
        assert m.mode == "rw"

    def test_immutable(self):
        m = agent.Mount(src="/host/x", dst="/cont/x")
        with pytest.raises(Exception):
            m.mode = "ro"  # type: ignore[misc]

    def test_equality(self):
        a = agent.Mount(src="/x", dst="/y", mode="ro")
        b = agent.Mount(src="/x", dst="/y", mode="ro")
        assert a == b


class TestMountToDockerArgs:
    def test_rw_bind_with_explicit_dst(self):
        m = agent.Mount(src="/host/a", dst="/cont/b", mode="rw")
        assert agent.mount_to_docker_args(m) == ["-v", "/host/a:/cont/b:rw"]

    def test_ro_bind(self):
        m = agent.Mount(src="/host/a", dst="/cont/b", mode="ro")
        assert agent.mount_to_docker_args(m) == ["-v", "/host/a:/cont/b:ro"]

    def test_bind_with_none_dst_mirrors_src(self):
        m = agent.Mount(src="/same/path", dst=None, mode="rw")
        assert agent.mount_to_docker_args(m) == ["-v", "/same/path:/same/path:rw"]

    def test_tmpfs(self):
        m = agent.Mount(src=None, dst="/cont/tmp", mode="tmpfs")
        assert agent.mount_to_docker_args(m) == ["--tmpfs", "/cont/tmp"]

    def test_tmpfs_without_dst_raises(self):
        m = agent.Mount(src=None, dst=None, mode="tmpfs")
        with pytest.raises(ValueError, match="tmpfs Mount requires dst"):
            agent.mount_to_docker_args(m)

    def test_bind_without_src_raises(self):
        m = agent.Mount(src=None, dst="/cont/x", mode="rw")
        with pytest.raises(ValueError, match="bind Mount requires src"):
            agent.mount_to_docker_args(m)
