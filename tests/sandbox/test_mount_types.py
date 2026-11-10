import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from seekr_hatchery.sandbox.mount_types import (
    BindMount,
    Mount,
    TmpfsMount,
    VolumeMount,
)


@pytest.mark.parametrize(
    "modules",
    [
        ["seekr_hatchery.sandbox.launch", "seekr_hatchery.sandbox.volumes", "seekr_hatchery.agents"],
        ["seekr_hatchery.agents", "seekr_hatchery.sidecars", "seekr_hatchery.sandbox.mount_types"],
        ["seekr_hatchery.sandbox.mount_types", "seekr_hatchery.sidecars", "seekr_hatchery.agents"],
        ["seekr_hatchery.sidecars", "seekr_hatchery.sandbox.mount_types", "seekr_hatchery.agents"],
    ],
)
def test_import_order(modules: list[str]) -> None:
    code = f"""
import importlib
for name in {modules!r} + ["seekr_hatchery.sandbox.config", "seekr_hatchery.sandbox.scaffold", "seekr_hatchery.sandbox.spec", "seekr_hatchery.sandbox.runtime", "seekr_hatchery.sandbox.images", "seekr_hatchery.sandbox.mounts", "seekr_hatchery.sandbox.volumes", "seekr_hatchery.sandbox.launch"]:
    importlib.import_module(name)
import seekr_hatchery.sandbox.mount_types as legacy
import seekr_hatchery.sandbox as sandbox
import seekr_hatchery.sandbox.mount_types as types
for name in sandbox.__all__:
    assert getattr(legacy, name) is getattr(types, name) is getattr(sandbox, name)
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_package_root_is_import_light() -> None:
    code = """
import sys
import seekr_hatchery.sandbox
assert not any(name in sys.modules for name in (
    'seekr_hatchery.agents', 'seekr_hatchery.sidecars',
    'seekr_hatchery.sandbox.config', 'seekr_hatchery.sandbox.scaffold',
    'seekr_hatchery.sandbox.spec', 'seekr_hatchery.sandbox.launch',
))
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


# ── Construction ──────────────────────────────────────────────────────────────


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


# ── Construction ──────────────────────────────────────────────────────────────


class TestTmpfsMount:
    def test_basic(self):
        m = TmpfsMount(dst="/tmp/cache")
        assert m.kind == "TMPFS"
        assert m.dst == "/tmp/cache"

    def test_dst_required(self):
        with pytest.raises(ValidationError):
            TmpfsMount()  # type: ignore[call-arg]


# ── Construction ──────────────────────────────────────────────────────────────


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

    def test_follow_links_defaults_off(self):
        assert BindMount(src=Path("/h"), dst="/c").follow_links is False

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
