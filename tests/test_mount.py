"""Unit tests for the Mount tagged-union types + mount_to_docker_args."""

from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from seekr_hatchery.mount import (
    BindMount,
    Mount,
    TmpfsMount,
    VolumeMount,
    file_mount_prestart_cmds,
    mount_to_docker_args,
    wrap_cmd_for_file_mounts,
)

# ── Construction ──────────────────────────────────────────────────────────────


class TestBindMount:
    def test_basic(self):
        m = BindMount(src=Path("/host/x"), dst="/cont/x")
        assert m.kind == "BIND"
        assert m.src == Path("/host/x")
        assert m.dst == "/cont/x"
        assert m.mode == "RW"  # default

    def test_ro_mode(self):
        m = BindMount(src=Path("/h"), dst="/c", mode="RO")
        assert m.mode == "RO"

    def test_dst_required(self):
        with pytest.raises(ValidationError):
            BindMount(src=Path("/h"))  # type: ignore[call-arg]

    def test_src_required(self):
        with pytest.raises(ValidationError):
            BindMount(dst="/c")  # type: ignore[call-arg]

    def test_invalid_mode_rejected(self):
        # Lowercase "rw" not accepted — ALL_CAPS only.
        with pytest.raises(ValidationError):
            BindMount(src=Path("/h"), dst="/c", mode="rw")  # type: ignore[arg-type]

    def test_immutable(self):
        m = BindMount(src=Path("/h"), dst="/c")
        with pytest.raises(ValidationError):
            m.mode = "RO"  # type: ignore[misc]

    def test_equality(self):
        a = BindMount(src=Path("/x"), dst="/y", mode="RO")
        b = BindMount(src=Path("/x"), dst="/y", mode="RO")
        assert a == b


class TestVolumeMount:
    def test_basic(self):
        m = VolumeMount(name="vol-a", dst="/cont")
        assert m.kind == "VOLUME"
        assert m.name == "vol-a"
        assert m.dst == "/cont"
        assert m.mode == "RW"
        assert m.is_file is False
        assert m.seed is None
        assert m.task_scoped is True  # default

    def test_is_file_true(self):
        m = VolumeMount(name="cfg", dst="/home/h/.cfg.json", is_file=True)
        assert m.is_file is True

    def test_task_scoped_false_for_user_volumes(self):
        # User-config volumes from docker.yaml: the name is the runtime
        # name verbatim, no per-task suffix.
        m = VolumeMount(name="hatchery-uv-cache", dst="/cache", task_scoped=False)
        assert m.task_scoped is False

    def test_seed_callable_accepted(self):
        def seed(ctx):
            return b"x"

        m = VolumeMount(name="v", dst="/c", is_file=True, seed=seed)
        assert m.seed is seed

    def test_immutable(self):
        m = VolumeMount(name="v", dst="/c")
        with pytest.raises(ValidationError):
            m.name = "other"  # type: ignore[misc]


class TestTmpfsMount:
    def test_basic(self):
        m = TmpfsMount(dst="/tmp/cache")
        assert m.kind == "TMPFS"
        assert m.dst == "/tmp/cache"

    def test_dst_required(self):
        with pytest.raises(ValidationError):
            TmpfsMount()  # type: ignore[call-arg]


# ── Discriminated union routing ───────────────────────────────────────────────


class TestDiscriminator:
    """The ``Mount`` alias is ``Annotated[Union, Field(discriminator='kind')]``.
    TypeAdapter uses the ``kind`` field to construct the right variant."""

    def test_bind(self):
        m = TypeAdapter(Mount).validate_python({"kind": "BIND", "src": "/host", "dst": "/cont", "mode": "RO"})
        assert isinstance(m, BindMount)
        assert m.src == Path("/host")
        assert m.mode == "RO"

    def test_volume(self):
        m = TypeAdapter(Mount).validate_python({"kind": "VOLUME", "name": "vol-a", "dst": "/cont", "is_file": True})
        assert isinstance(m, VolumeMount)
        assert m.name == "vol-a"
        assert m.is_file is True

    def test_tmpfs(self):
        m = TypeAdapter(Mount).validate_python({"kind": "TMPFS", "dst": "/tmp"})
        assert isinstance(m, TmpfsMount)

    def test_unknown_kind_rejected(self):
        with pytest.raises(ValidationError):
            TypeAdapter(Mount).validate_python({"kind": "WAT", "dst": "/c"})


# ── CLI serialisation ─────────────────────────────────────────────────────────


class TestMountToDockerArgs:
    def test_bind_rw(self):
        m = BindMount(src=Path("/host/a"), dst="/cont/b", mode="RW")
        assert mount_to_docker_args(m) == ["-v", "/host/a:/cont/b:rw"]

    def test_bind_ro(self):
        m = BindMount(src=Path("/host/a"), dst="/cont/b", mode="RO")
        assert mount_to_docker_args(m) == ["-v", "/host/a:/cont/b:ro"]

    def test_tmpfs(self):
        m = TmpfsMount(dst="/cont/tmp")
        assert mount_to_docker_args(m) == ["--tmpfs", "/cont/tmp"]

    def test_volume_dir_shape(self):
        # Directory-shaped: plain -v with the runtime volume name.
        m = VolumeMount(name="hatchery-x-vol-state", dst="/home/h/.state", mode="RW")
        assert mount_to_docker_args(m) == ["-v", "hatchery-x-vol-state:/home/h/.state:rw"]

    def test_volume_dir_shape_ro(self):
        m = VolumeMount(name="vol-a", dst="/d", mode="RO")
        assert mount_to_docker_args(m) == ["-v", "vol-a:/d:ro"]

    def test_volume_file_shape_uses_sidecar(self):
        """File-shaped volume mounts use a sidecar directory (dst + ".vol") so
        the volume (which the kernel always presents as a directory) doesn't
        shadow the parent directory.  A symlink from dst into the sidecar is
        created at container startup via file_mount_prestart_cmds."""
        m = VolumeMount(
            name="hatchery-x-vol-cfg",
            dst="/home/h/.cfg.json",
            is_file=True,
            mode="RW",
        )
        assert mount_to_docker_args(m) == ["-v", "hatchery-x-vol-cfg:/home/h/.cfg.json.vol:rw"]

    def test_volume_file_shape_ro(self):
        m = VolumeMount(name="v", dst="/home/h/.cfg.json", is_file=True, mode="RO")
        assert mount_to_docker_args(m) == ["-v", "v:/home/h/.cfg.json.vol:ro"]


class TestFileMountPrestart:
    def test_empty_when_no_file_mounts(self):
        mounts = [
            BindMount(src=Path("/host"), dst="/cont"),
            VolumeMount(name="vol-state", dst="/home/h/.state"),
            TmpfsMount(dst="/tmp"),
        ]
        assert file_mount_prestart_cmds(mounts) == []

    def test_single_file_mount(self):
        mounts = [VolumeMount(name="vol-cfg", dst="/home/h/.cfg.json", is_file=True)]
        cmds = file_mount_prestart_cmds(mounts)
        assert cmds == ["ln -sf /home/h/.cfg.json.vol/.cfg.json /home/h/.cfg.json"]

    def test_multiple_file_mounts(self):
        mounts = [
            VolumeMount(name="vol-a", dst="/home/h/.a.json", is_file=True),
            VolumeMount(name="vol-b", dst="/home/h/.b.json", is_file=True),
        ]
        cmds = file_mount_prestart_cmds(mounts)
        assert cmds == [
            "ln -sf /home/h/.a.json.vol/.a.json /home/h/.a.json",
            "ln -sf /home/h/.b.json.vol/.b.json /home/h/.b.json",
        ]

    def test_mixed_mounts(self):
        mounts = [
            BindMount(src=Path("/host"), dst="/cont"),
            VolumeMount(name="vol-cfg", dst="/home/h/.cfg.json", is_file=True),
            VolumeMount(name="vol-state", dst="/home/h/.state"),
        ]
        cmds = file_mount_prestart_cmds(mounts)
        assert cmds == ["ln -sf /home/h/.cfg.json.vol/.cfg.json /home/h/.cfg.json"]


class TestWrapCmdForFileMounts:
    def test_passthrough_when_no_file_mounts(self):
        """No file-shaped mounts → cmd is returned unchanged. No sh -c wrap,
        no symlink setup, no shell evaluation surface added to the command."""
        mounts = [
            BindMount(src=Path("/host"), dst="/cont"),
            VolumeMount(name="vol-state", dst="/home/h/.state"),
        ]
        cmd = ["claude", "--resume=abc", "go fix the bug"]
        assert wrap_cmd_for_file_mounts(cmd, mounts) == cmd

    def test_wraps_with_sh_when_file_mount_present(self):
        """File-shaped mount present → cmd is wrapped in ``sh -c '<ln> && exec <cmd>'``
        so the symlink is in place before the agent process is exec'd. The
        original cmd is shlex-joined so args with spaces/quotes round-trip."""
        mounts = [VolumeMount(name="vol-cfg", dst="/home/h/.cfg.json", is_file=True)]
        cmd = ["claude", "--prompt", "hello world"]
        out = wrap_cmd_for_file_mounts(cmd, mounts)
        assert out == [
            "sh",
            "-c",
            "ln -sf /home/h/.cfg.json.vol/.cfg.json /home/h/.cfg.json && exec claude --prompt 'hello world'",
        ]

    def test_multiple_file_mounts_chain_with_and(self):
        mounts = [
            VolumeMount(name="vol-a", dst="/home/h/.a.json", is_file=True),
            VolumeMount(name="vol-b", dst="/home/h/.b.json", is_file=True),
        ]
        out = wrap_cmd_for_file_mounts(["agent"], mounts)
        assert out == [
            "sh",
            "-c",
            "ln -sf /home/h/.a.json.vol/.a.json /home/h/.a.json"
            " && ln -sf /home/h/.b.json.vol/.b.json /home/h/.b.json"
            " && exec agent",
        ]
