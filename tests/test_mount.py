"""Unit tests for the Mount dataclass and mount_to_docker_args helper."""

import pytest

import seekr_hatchery.mount as mount


class TestMount:
    def test_mode_is_required(self):
        # Omitting mode must raise — there is no default.  Guards against a
        # caller forgetting an arg and silently getting a read-write bind.
        with pytest.raises(TypeError):
            mount.Mount(src="/host/x", dst="/cont/x")  # type: ignore[call-arg]

    def test_dst_defaults_to_none(self):
        m = mount.Mount(src="/host/x", mode="rw")
        assert m.dst is None

    def test_immutable(self):
        m = mount.Mount(src="/host/x", dst="/cont/x", mode="rw")
        with pytest.raises(Exception):
            m.mode = "ro"  # type: ignore[misc]

    def test_equality(self):
        a = mount.Mount(src="/x", dst="/y", mode="ro")
        b = mount.Mount(src="/x", dst="/y", mode="ro")
        assert a == b


class TestMountToDockerArgs:
    def test_rw_bind_with_explicit_dst(self):
        m = mount.Mount(src="/host/a", dst="/cont/b", mode="rw")
        assert mount.mount_to_docker_args(m) == ["-v", "/host/a:/cont/b:rw"]

    def test_ro_bind(self):
        m = mount.Mount(src="/host/a", dst="/cont/b", mode="ro")
        assert mount.mount_to_docker_args(m) == ["-v", "/host/a:/cont/b:ro"]

    def test_bind_with_none_dst_mirrors_src(self):
        m = mount.Mount(src="/same/path", dst=None, mode="rw")
        assert mount.mount_to_docker_args(m) == ["-v", "/same/path:/same/path:rw"]

    def test_tmpfs(self):
        m = mount.Mount(src=None, dst="/cont/tmp", mode="tmpfs")
        assert mount.mount_to_docker_args(m) == ["--tmpfs", "/cont/tmp"]

    def test_tmpfs_without_dst_raises(self):
        m = mount.Mount(src=None, dst=None, mode="tmpfs")
        with pytest.raises(ValueError, match="tmpfs Mount requires dst"):
            mount.mount_to_docker_args(m)

    def test_bind_without_src_raises(self):
        m = mount.Mount(src=None, dst="/cont/x", mode="rw")
        with pytest.raises(ValueError, match="bind Mount requires src"):
            mount.mount_to_docker_args(m)
