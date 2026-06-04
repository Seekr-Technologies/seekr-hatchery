"""Tests for the seeded-volume lifecycle (creation, seeding, cleanup).

Where the lifecycle touches a runtime (creating volumes, streaming seed
content via a helper container), tests prefer real podman over mocking
to validate actual behavior. Mark those with ``@pytest.mark.integration``
— they're opt-in via ``pytest --integration``. Unit tests cover the
pure logic (name resolution, error paths, dispatch) without subprocess.
"""

import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from seekr_hatchery.mount import (
    BindMount,
    SeedContext,
    TmpfsMount,
    VolumeMount,
)
from seekr_hatchery.seeded_volumes import (
    _seed_files_for,
    cleanup_task_volumes,
    prepare_volume_mounts,
    task_volume_prefix,
    volume_name,
)

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
        assert volume_name(_fake_meta(container_name="hatchery-foo-bar"), "app-cfg") == (
            "hatchery-foo-bar-vol-app-cfg"
        )

    def test_prefix_covers_volume_names(self, tmp_path):
        # task_volume_prefix is what cleanup_task_volumes enumerates;
        # any volume_name() result should start with the prefix.
        repo = tmp_path / "repo"
        repo.mkdir()
        prefix = task_volume_prefix(repo, "mytask")
        meta = _fake_meta(container_name=prefix[:-5])  # strip the trailing "-vol-"
        assert volume_name(meta, "any").startswith(prefix)


# ─── Pure: _seed_files_for normalisation ──────────────────────────────────────


class TestSeedFilesFor:
    def test_is_file_returns_bytes_keyed_at_basename(self):
        m = VolumeMount(
            name="cfg", dst="/home/h/.foo.json", is_file=True,
            seed=lambda ctx: b'{"x":1}',
        )
        out = _seed_files_for(m, _seed_ctx())
        assert out == {".foo.json": b'{"x":1}'}

    def test_dir_returns_mapping_verbatim(self):
        payload = {"a.txt": b"a", "b/c.txt": b"c"}
        m = VolumeMount(name="d", dst="/d", seed=lambda ctx: payload)
        assert _seed_files_for(m, _seed_ctx()) == payload

    def test_is_file_rejects_mapping(self):
        m = VolumeMount(name="c", dst="/c", is_file=True, seed=lambda ctx: {"x": b"y"})
        with pytest.raises(TypeError, match="must return bytes"):
            _seed_files_for(m, _seed_ctx())

    def test_dir_rejects_bytes(self):
        m = VolumeMount(name="d", dst="/d", is_file=False, seed=lambda ctx: b"x")
        with pytest.raises(TypeError, match="Mapping"):
            _seed_files_for(m, _seed_ctx())

    def test_seed_receives_context(self):
        captured = []
        m = VolumeMount(name="c", dst="/c", is_file=True,
                        seed=lambda ctx: (captured.append(ctx), b"x")[1])
        _seed_files_for(m, _seed_ctx())
        assert captured[0].proxy_token == "t0k3n"
        assert captured[0].container_workdir == "/workspace"


# ─── prepare_volume_mounts pass-through ───────────────────────────────────────


class TestPrepareVolumeMountsPassThrough:
    """Non-VolumeMount entries must pass through unchanged with no runtime
    calls. No podman needed — verify by passing a runtime_binary that
    would fail if invoked."""

    def test_bind_passes_through(self):
        m = BindMount(src=Path("/h"), dst="/c")
        out = prepare_volume_mounts(
            "/no/such/binary", [m], _fake_meta(), Path("/s"), "tok", "/wd",
        )
        assert out == [m]

    def test_tmpfs_passes_through(self):
        m = TmpfsMount(dst="/tmp/x")
        out = prepare_volume_mounts(
            "/no/such/binary", [m], _fake_meta(), Path("/s"), "tok", "/wd",
        )
        assert out == [m]


# ─── Integration: full lifecycle against real podman ─────────────────────────


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


# ─── cleanup_task_volumes ─────────────────────────────────────────────────────


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


class TestCleanupTaskVolumesNoRuntime:
    """If neither podman nor docker is reachable, cleanup must silently
    no-op — ``hatchery rm`` should not crash because the runtime is
    offline."""

    def test_silent_when_no_runtime(self, monkeypatch, tmp_path):
        import seekr_hatchery.docker as docker
        monkeypatch.setattr(docker, "podman_available", lambda: False)
        monkeypatch.setattr(docker, "docker_available", lambda: False)
        repo = tmp_path / "r"
        repo.mkdir()
        # Should return without raising.
        cleanup_task_volumes(repo, "anything")
