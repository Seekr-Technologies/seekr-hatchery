import io
import shutil
import subprocess
import tarfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import seekr_hatchery.models as models
import seekr_hatchery.sandbox.mount_types as mount_types
import seekr_hatchery.sandbox.runtime as sandbox_runtime
import seekr_hatchery.sandbox.volumes as volumes
import seekr_hatchery.utils as utils
from seekr_hatchery.sandbox.mount_types import (
    BindMount,
    SeedContext,
    TmpfsMount,
    VolumeMount,
)
from seekr_hatchery.sandbox.volumes import (
    cleanup_task_volumes,
    prepare_volume_mounts,
    task_volume_prefix,
    volume_name,
)


@pytest.mark.parametrize("exists", [False, True])
def test_prepare_volume_mounts_seed_once(tmp_path: Path, monkeypatch, exists: bool) -> None:
    meta = models.SessionMeta(name="task", repo=str(tmp_path), worktree=str(tmp_path), no_worktree=True)
    calls: list[list[str]] = []
    seeded: list[bytes] = []
    contexts: list[mount_types.SeedContext] = []

    def run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0 if exists or argv[2] != "inspect" else 1, stdout="")

    def seed_run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
        calls.append(argv)
        seeded.append(kwargs["input"])
        return subprocess.CompletedProcess(argv, 0)

    def seed(ctx: mount_types.SeedContext) -> bytes:
        contexts.append(ctx)
        return b"initial state"

    monkeypatch.setattr(utils, "run", run)
    monkeypatch.setattr(subprocess, "run", seed_run)
    mount = mount_types.VolumeMount(name="state", dst="/home/hatchery/state.json", is_file=True, seed=seed)
    bind = mount_types.BindMount(src=tmp_path, dst="/workspace")
    name = volumes.volume_name(meta, "state")
    assert volumes.prepare_volume_mounts("podman", [bind, mount], meta, tmp_path, "token", "/workspace") == [
        bind,
        mount.model_copy(update={"name": name}),
    ]
    assert contexts == ([] if exists else [mount_types.SeedContext(tmp_path, "token", "/workspace")])
    expected = [["podman", "volume", "inspect", name]]
    if not exists:
        expected.extend(
            [
                ["podman", "volume", "create", name],
                [
                    "podman",
                    "run",
                    "--rm",
                    "-i",
                    "--user",
                    "0:0",
                    "-v",
                    f"{name}:/seed",
                    meta.image_name,
                    "sh",
                    "-c",
                    "tar -xf - -C /seed && chown -R 1000:1000 /seed",
                ],
            ]
        )
        with tarfile.open(fileobj=io.BytesIO(seeded[0])) as archive:
            assert {member.name: archive.extractfile(member).read() for member in archive} == {
                "state.json": b"initial state"
            }
    assert calls == expected


def test_prepare_volume_mounts_rolls_back_failed_seed(tmp_path: Path, monkeypatch) -> None:
    meta = models.SessionMeta(name="task", repo=str(tmp_path), worktree=str(tmp_path), no_worktree=True)
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1 if argv[2] == "inspect" else 0, stdout="")

    def seed(ctx: mount_types.SeedContext) -> bytes:
        raise ValueError("seed failed")

    monkeypatch.setattr(utils, "run", run)
    mount = mount_types.VolumeMount(name="state", dst="/state", is_file=True, seed=seed)
    with pytest.raises(ValueError, match="^seed failed$"):
        volumes.prepare_volume_mounts("docker", [mount], meta, tmp_path, "token", "/workspace")
    name = volumes.volume_name(meta, "state")
    assert calls == [
        ["docker", "volume", "inspect", name],
        ["docker", "volume", "create", name],
        ["docker", "volume", "rm", "--force", name],
    ]


INTEGRATION_IMAGE = "alpine"  # minimal image for the helper container


# ─── Pure: name derivation ────────────────────────────────────────────────────


class TestCleanupTaskVolumesNoRuntime:
    """If neither podman nor docker is reachable, cleanup must silently
    no-op — ``hatchery rm`` should not crash because the runtime is
    offline."""

    def test_silent_when_no_runtime(self, monkeypatch, tmp_path):

        monkeypatch.setattr(sandbox_runtime.PodmanRuntime, "available", staticmethod(lambda: False))
        monkeypatch.setattr(sandbox_runtime.DockerRuntime, "available", staticmethod(lambda: False))
        repo = tmp_path / "r"
        repo.mkdir()
        # Should return without raising.
        cleanup_task_volumes(repo, "anything")


INTEGRATION_IMAGE = "alpine"  # minimal image for the helper container


# ─── Pure: name derivation ────────────────────────────────────────────────────


@pytest.mark.integration
class TestCleanupTaskVolumesIntegration:
    def test_removes_matching_only(self, real_podman, tmp_path):
        """Create three volumes: two prefixed for this task, one unrelated.
        Cleanup removes only the matching pair."""
        if not _podman_available():
            pytest.skip("podman not available")
        repo = tmp_path / "repo"
        repo.mkdir()
        name = f"task-{uuid.uuid4().hex[:6]}"
        prefix = task_volume_prefix(repo, name)
        matching = [f"{prefix}app-cfg", f"{prefix}app-dir"]
        unrelated = f"hatchery-unrelated-{uuid.uuid4().hex[:6]}"
        for v in matching + [unrelated]:
            subprocess.run(["podman", "volume", "create", v], capture_output=True, check=True)
        try:
            cleanup_task_volumes(repo, name)
            # Matching volumes gone.
            for v in matching:
                r = subprocess.run(["podman", "volume", "inspect", v], capture_output=True)
                assert r.returncode != 0, f"{v} should have been removed"
            # Unrelated volume preserved.
            r = subprocess.run(["podman", "volume", "inspect", unrelated], capture_output=True)
            assert r.returncode == 0, "unrelated volume must not be touched"
        finally:
            for v in [*matching, unrelated]:
                _remove_volume(v)


INTEGRATION_IMAGE = "alpine"  # minimal image for the helper container


# ─── Pure: name derivation ────────────────────────────────────────────────────


@pytest.mark.integration
class TestPrepareVolumeMountsIntegration:
    def test_creates_volume_and_seeds_on_first_call(self, real_podman):
        container = real_podman
        meta = _fake_meta(container_name=container)
        m = VolumeMount(
            name="state",
            dst="/home/hatchery/.state.json",
            is_file=True,
            seed=lambda ctx: f'{{"token":"{ctx.proxy_token}"}}'.encode(),
        )
        try:
            out = prepare_volume_mounts("podman", [m], meta, Path("/s"), "t0k3n", "/wd")
            assert len(out) == 1
            resolved = out[0]
            assert isinstance(resolved, VolumeMount)
            assert resolved.name == f"{container}-vol-state"
            # The file landed in the volume at basename(dst).
            assert _read_volume_file(resolved.name, ".state.json") == b'{"token":"t0k3n"}'
        finally:
            _remove_volume(f"{container}-vol-state")

    def test_seed_does_not_rerun_on_resume(self, real_podman):
        """Second call against the same task: volume exists, seed must NOT
        re-fire. This is the contract that lets ``hatchery resume`` keep
        accumulated state intact."""
        container = real_podman
        meta = _fake_meta(container_name=container)
        call_count = {"n": 0}

        def _seed(ctx):
            call_count["n"] += 1
            return b'{"v":1}'

        m = VolumeMount(name="state", dst="/home/h/.state.json", is_file=True, seed=_seed)
        try:
            prepare_volume_mounts("podman", [m], meta, Path("/s"), "t", "/wd")
            assert call_count["n"] == 1
            prepare_volume_mounts("podman", [m], meta, Path("/s"), "t", "/wd")
            assert call_count["n"] == 1, "seed re-ran on resume"
        finally:
            _remove_volume(f"{container}-vol-state")

    def test_user_volume_is_not_task_scoped(self, real_podman):
        """User-config volumes (task_scoped=False) keep their literal name
        — no per-task prefix. The runtime volume name we created on
        first call must equal the spec name verbatim."""
        container = real_podman
        meta = _fake_meta(container_name=container)
        user_vol_name = f"hatchery-user-cache-{uuid.uuid4().hex[:6]}"
        m = VolumeMount(name=user_vol_name, dst="/cache", task_scoped=False)
        try:
            out = prepare_volume_mounts("podman", [m], meta, Path("/s"), "t", "/wd")
            assert out[0].name == user_vol_name
            res = subprocess.run(["podman", "volume", "inspect", user_vol_name], capture_output=True)
            assert res.returncode == 0, "user volume was not created"
        finally:
            _remove_volume(user_vol_name)

    def test_dir_shaped_volume_round_trip(self, real_podman):
        """is_file=False: seed returns {relpath: bytes}; each file lands at
        that relpath in the volume."""
        container = real_podman
        meta = _fake_meta(container_name=container)
        payload = {"a.txt": b"alpha", "sub/b.txt": b"beta"}
        m = VolumeMount(name="dir", dst="/home/h/.d", seed=lambda ctx: payload)
        try:
            out = prepare_volume_mounts("podman", [m], meta, Path("/s"), "t", "/wd")
            assert _read_volume_file(out[0].name, "a.txt") == b"alpha"
            assert _read_volume_file(out[0].name, "sub/b.txt") == b"beta"
        finally:
            _remove_volume(f"{container}-vol-dir")

    def test_rollback_removes_volume_when_seed_raises(self, real_podman):
        """A half-created volume must be removed so the next launch
        retries the seed cleanly rather than mounting empty state."""
        container = real_podman
        meta = _fake_meta(container_name=container)

        def _broken_seed(ctx):
            raise RuntimeError("intentional seed failure")

        m = VolumeMount(name="rb", dst="/h.json", is_file=True, seed=_broken_seed)
        try:
            with pytest.raises(RuntimeError, match="intentional seed failure"):
                prepare_volume_mounts("podman", [m], meta, Path("/s"), "t", "/wd")
            # The volume should not exist after rollback.
            res = subprocess.run(
                ["podman", "volume", "inspect", f"{container}-vol-rb"],
                capture_output=True,
            )
            assert res.returncode != 0, "volume should have been removed by rollback"
        finally:
            _remove_volume(f"{container}-vol-rb")


INTEGRATION_IMAGE = "alpine"  # minimal image for the helper container


# ─── Pure: name derivation ────────────────────────────────────────────────────


class TestPrepareVolumeMountsPassThrough:
    """Non-VolumeMount entries must pass through unchanged with no runtime
    calls. No podman needed — verify by passing a runtime_binary that
    would fail if invoked."""

    def test_bind_passes_through(self):
        m = BindMount(src=Path("/h"), dst="/c")
        out = prepare_volume_mounts(
            "/no/such/binary",
            [m],
            _fake_meta(),
            Path("/s"),
            "tok",
            "/wd",
        )
        assert out == [m]

    def test_tmpfs_passes_through(self):
        m = TmpfsMount(dst="/tmp/x")
        out = prepare_volume_mounts(
            "/no/such/binary",
            [m],
            _fake_meta(),
            Path("/s"),
            "tok",
            "/wd",
        )
        assert out == [m]


INTEGRATION_IMAGE = "alpine"  # minimal image for the helper container


# ─── Pure: name derivation ────────────────────────────────────────────────────


class TestSeedFilesFor:
    def test_is_file_returns_bytes_keyed_at_basename(self):
        m = VolumeMount(
            name="cfg",
            dst="/home/h/.foo.json",
            is_file=True,
            seed=lambda ctx: b'{"x":1}',
        )
        out = _seeded_payload(m, _seed_ctx())
        assert out == {".foo.json": b'{"x":1}'}

    def test_dir_returns_mapping_verbatim(self):
        payload = {"a.txt": b"a", "b/c.txt": b"c"}
        m = VolumeMount(name="d", dst="/d", seed=lambda ctx: payload)
        assert _seeded_payload(m, _seed_ctx()) == payload

    def test_is_file_rejects_mapping(self):
        m = VolumeMount(name="c", dst="/c", is_file=True, seed=lambda ctx: {"x": b"y"})
        with pytest.raises(TypeError) as exc:
            _seeded_payload(m, _seed_ctx())
        assert str(exc.value) == "VolumeMount(is_file=True) seed must return bytes; got dict"

    def test_dir_rejects_bytes(self):
        m = VolumeMount(name="d", dst="/d", is_file=False, seed=lambda ctx: b"x")
        with pytest.raises(TypeError) as exc:
            _seeded_payload(m, _seed_ctx())
        assert str(exc.value) == "VolumeMount(is_file=False) seed must return Mapping[str, bytes]; got bytes"

    def test_seed_receives_context(self):
        captured = []
        m = VolumeMount(name="c", dst="/c", is_file=True, seed=lambda ctx: (captured.append(ctx), b"x")[1])
        _seeded_payload(m, _seed_ctx())
        assert captured[0].proxy_token == "t0k3n"
        assert captured[0].container_workdir == "/workspace"


INTEGRATION_IMAGE = "alpine"  # minimal image for the helper container


def _fake_meta(container_name="hatchery-r-task", image_name=INTEGRATION_IMAGE):
    """Lightweight stand-in for SessionMeta — only container_name and
    image_name are used by the lifecycle."""
    return SimpleNamespace(container_name=container_name, image_name=image_name)


def _seed_ctx():
    return SeedContext(
        session_dir=Path("/tmp/session"),
        proxy_token="t0k3n",
        container_workdir="/workspace",
    )


def _podman_available() -> bool:
    if shutil.which("podman") is None:
        return False
    return subprocess.run(["podman", "info"], capture_output=True).returncode == 0


def _ensure_image(image: str) -> None:
    """Pull the image if not local. Skip the test if pull fails."""
    if subprocess.run(["podman", "image", "exists", image], capture_output=True).returncode == 0:
        return
    pull = subprocess.run(["podman", "pull", "-q", image], capture_output=True, text=True)
    if pull.returncode != 0:
        pytest.skip(f"could not pull {image}: {pull.stderr.strip()}")


# ─── Pure: name derivation ────────────────────────────────────────────────────


class TestVolumeName:
    def test_format(self):
        assert volume_name(_fake_meta(container_name="hatchery-foo-bar"), "app-cfg") == ("hatchery-foo-bar-vol-app-cfg")

    def test_prefix_covers_volume_names(self, tmp_path):
        # task_volume_prefix is what cleanup_task_volumes enumerates;
        # any volume_name() result should start with the prefix.
        repo = tmp_path / "repo"
        repo.mkdir()
        prefix = task_volume_prefix(repo, "mytask")
        meta = _fake_meta(container_name=prefix[:-5])  # strip the trailing "-vol-"
        assert volume_name(meta, "any").startswith(prefix)


@pytest.fixture
def real_podman():
    """Skip the test if podman isn't reachable; otherwise ensure the
    helper image is local and provide a unique container_name for the
    test so volumes don't collide between concurrent runs."""
    if not _podman_available():
        pytest.skip("podman not available")
    _ensure_image(INTEGRATION_IMAGE)
    yield f"hatchery-test-{uuid.uuid4().hex[:8]}"


def _read_volume_file(volume: str, path: str) -> bytes:
    """Spawn alpine, mount the volume, cat the file. Returns stdout."""
    res = subprocess.run(
        ["podman", "run", "--rm", "-v", f"{volume}:/v", INTEGRATION_IMAGE, "cat", f"/v/{path}"],
        capture_output=True,
    )
    assert res.returncode == 0, f"read failed: {res.stderr!r}"
    return res.stdout


def _remove_volume(volume: str) -> None:
    subprocess.run(["podman", "volume", "rm", "--force", volume], capture_output=True)


def _seeded_payload(mount: VolumeMount, context: SeedContext) -> dict[str, bytes]:
    from unittest.mock import patch

    payload: dict[str, bytes] = {}

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, 1 if argv[2] == "inspect" else 0)

    def seed_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        with tarfile.open(fileobj=io.BytesIO(kwargs["input"])) as archive:
            payload.update({member.name: archive.extractfile(member).read() for member in archive})
        return subprocess.CompletedProcess(argv, 0)

    with patch.object(utils, "run", run), patch.object(subprocess, "run", seed_run):
        prepare_volume_mounts(
            "podman", [mount], _fake_meta(), context.session_dir, context.proxy_token, context.container_workdir
        )
    return payload
