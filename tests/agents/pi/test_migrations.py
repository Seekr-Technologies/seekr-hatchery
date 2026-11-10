"""Public Pi storage upgrade contracts and live recovery regressions."""

import json
import multiprocessing
import os
import subprocess
import threading
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import seekr_hatchery.agents as agent
import seekr_hatchery.agents.pi.backend as pi_backend
import seekr_hatchery.agents.pi.migrations as migrations
import seekr_hatchery.docker as docker
import seekr_hatchery.mount as mount
import seekr_hatchery.seeded_volumes as seeded_volumes

PI_TEST_IMAGE = "docker.io/library/alpine:3.21"


COMPLETED = {"layout_version": 2, "migration": None}
IN_PROGRESS = {
    "layout_version": 1,
    "migration": {"from_version": 1, "to_version": 2, "status": "IN_PROGRESS"},
}


def _state(meta: SimpleNamespace) -> dict:
    paths = list((meta.session_dir / "pi-container-state").glob("*.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text())


@pytest.fixture
def legacy_pi_history(pi_container: SimpleNamespace) -> Generator[SimpleNamespace, None, None]:
    source = seeded_volumes.volume_name(pi_container, "pi-dir")
    destination = seeded_volumes.volume_name(pi_container, "pi-sessions")
    try:
        seeded_volumes.prepare_volume_mounts(
            "podman",
            [mount.VolumeMount(name="pi-dir", dst="/legacy", seed=lambda _: {"sessions/history.jsonl": b"history"})],
            pi_container,
            pi_container.session_dir,
            "token",
            "/work",
        )
        yield pi_container
    finally:
        helpers = subprocess.run(
            ["podman", "ps", "-a", "-q", "--filter", f"volume={destination}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        for helper in helpers:
            subprocess.run(["podman", "rm", "--force", helper], capture_output=True, check=True)
        _remove_pi_volume(destination)
        _remove_pi_volume(source)


def _read_pi_volume_file(volume: str, path: str) -> bytes:
    return subprocess.run(
        ["podman", "run", "--rm", "-v", f"{volume}:/v", PI_TEST_IMAGE, "cat", f"/v/{path}"],
        check=True,
        capture_output=True,
    ).stdout


def _remove_pi_volume(volume: str) -> None:
    subprocess.run(["podman", "volume", "rm", "--force", volume], capture_output=True)


@pytest.mark.integration
@pytest.mark.parametrize("legacy_state", ["missing", "no_sessions", "history"])
def test_pi_sessions_migrate_once(pi_container: SimpleNamespace, legacy_state: str) -> None:
    meta = pi_container
    source = seeded_volumes.volume_name(meta, "pi-dir")
    destination = seeded_volumes.volume_name(meta, "pi-sessions")
    mounts = [
        mount.VolumeMount(
            name="pi-sessions", dst="/home/hatchery/.pi/agent/sessions", seed=pi_backend._seed_empty_state
        )
    ]
    try:
        if legacy_state != "missing":
            files = {"auth.json": b"host-secret"}
            if legacy_state == "history":
                files["sessions/worktree/session.jsonl"] = b'{"message":"original history"}\n'
            seeded_volumes.prepare_volume_mounts(
                "podman",
                [mount.VolumeMount(name="pi-dir", dst="/legacy", seed=lambda ctx: files)],
                meta,
                Path("/s"),
                "tok",
                "/wd",
            )
        agent.PI.migrate_container_state(meta, runtime=docker.PodmanRuntime())
        assert seeded_volumes.prepare_volume_mounts("podman", mounts, meta, Path("/s"), "tok", "/wd") == [
            mount.VolumeMount(
                name=destination, dst="/home/hatchery/.pi/agent/sessions", seed=pi_backend._seed_empty_state
            )
        ]
        script = "test ! -e /new/auth.json; echo newer-history > /new/current.jsonl; "
        if legacy_state == "history":
            assert (
                _read_pi_volume_file(destination, "worktree/session.jsonl") == files["sessions/worktree/session.jsonl"]
            )
            assert (
                _read_pi_volume_file(source, "sessions/worktree/session.jsonl")
                == files["sessions/worktree/session.jsonl"]
            )
            # Deletions and edits after migration must not resurrect old history.
            script += "rm /new/worktree/session.jsonl; "
        subprocess.run(
            [
                "podman",
                "run",
                "--rm",
                "--user",
                "1000:1000",
                "-v",
                f"{destination}:/new",
                PI_TEST_IMAGE,
                "sh",
                "-ec",
                script,
            ],
            check=True,
            capture_output=True,
        )
        agent.PI.migrate_container_state(meta, runtime=docker.PodmanRuntime())
        assert _read_pi_volume_file(destination, "current.jsonl") == b"newer-history\n"
        if legacy_state == "history":
            subprocess.run(
                [
                    "podman",
                    "run",
                    "--rm",
                    "-v",
                    f"{destination}:/new",
                    PI_TEST_IMAGE,
                    "sh",
                    "-ec",
                    "test ! -e /new/worktree/session.jsonl",
                ],
                check=True,
                capture_output=True,
            )
    finally:
        _remove_pi_volume(destination)
        _remove_pi_volume(source)


@pytest.mark.integration
def test_failed_pi_migration_rolls_back_and_retries(
    pi_container: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = pi_container
    source = seeded_volumes.volume_name(meta, "pi-dir")
    destination = seeded_volumes.volume_name(meta, "pi-sessions")
    try:
        seeded_volumes.prepare_volume_mounts(
            "podman",
            [mount.VolumeMount(name="pi-dir", dst="/legacy", seed=lambda ctx: {"sessions/history.jsonl": b"history"})],
            meta,
            Path("/s"),
            "tok",
            "/wd",
        )
        real_run = migrations.utils.run

        def fail_after_copy(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
            if cmd[:2] == ["podman", "run"]:
                assert _state(meta) == IN_PROGRESS
            # Inject a real helper-process failure after it has written files,
            # exercising rollback of a populated (not merely empty) destination.
            if cmd[:2] == ["podman", "run"]:
                cmd = [*cmd[:-1], cmd[-1] + "; exit 1"]
            return real_run(cmd, check=check)

        with monkeypatch.context() as patch:
            patch.setattr(migrations.utils, "run", fail_after_copy)
            with pytest.raises(subprocess.CalledProcessError):
                agent.PI.migrate_container_state(meta, runtime=docker.PodmanRuntime())
        assert subprocess.run(["podman", "volume", "inspect", destination], capture_output=True).returncode != 0
        assert _read_pi_volume_file(source, "sessions/history.jsonl") == b"history"
        agent.PI.migrate_container_state(meta, runtime=docker.PodmanRuntime())
        assert _read_pi_volume_file(destination, "history.jsonl") == b"history"
    finally:
        _remove_pi_volume(destination)
        _remove_pi_volume(source)


@pytest.mark.integration
@pytest.mark.parametrize("failure", ["interrupt", "cleanup", "lost_source"])
def test_incomplete_migration_is_not_exposed(
    legacy_pi_history: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    meta = legacy_pi_history
    destination = seeded_volumes.volume_name(meta, "pi-sessions")
    source = seeded_volumes.volume_name(meta, "pi-dir")
    real_run = migrations.utils.run

    def fail_import(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["podman", "run"]:
            assert _state(meta) == IN_PROGRESS
            if failure == "interrupt":
                raise KeyboardInterrupt("interrupted import")
            cmd = [*cmd[:-1], cmd[-1] + "; echo stale > /sessions/stale; exit 1"]
        if failure != "interrupt" and cmd[:3] == ["podman", "volume", "rm"]:
            raise subprocess.CalledProcessError(1, cmd)
        return real_run(cmd, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(migrations.utils, "run", fail_import)
        expected_error = KeyboardInterrupt if failure == "interrupt" else subprocess.CalledProcessError
        with pytest.raises(expected_error):
            agent.PI.migrate_container_state(meta, runtime=docker.PodmanRuntime())
    assert _state(meta) == IN_PROGRESS
    if failure != "interrupt":
        assert _read_pi_volume_file(destination, "stale") == b"stale\n"
    if failure == "lost_source":
        _remove_pi_volume(source)
        with pytest.raises(RuntimeError, match="legacy history volume is missing"):
            agent.PI.migrate_container_state(meta, runtime=docker.PodmanRuntime())
        assert _read_pi_volume_file(destination, "stale") == b"stale\n"
        assert _state(meta) == IN_PROGRESS
        return

    agent.PI.migrate_container_state(meta, runtime=docker.PodmanRuntime())
    assert _state(meta) == COMPLETED
    assert _read_pi_volume_file(source, "sessions/history.jsonl") == b"history"
    assert _read_pi_volume_file(destination, "history.jsonl") == b"history"
    subprocess.run(
        [
            "podman",
            "run",
            "--rm",
            "--user",
            "1000:1000",
            "-v",
            f"{destination}:/new",
            PI_TEST_IMAGE,
            "sh",
            "-ec",
            "test ! -e /new/stale; echo newer > /new/history.jsonl",
        ],
        check=True,
        capture_output=True,
    )
    agent.PI.migrate_container_state(meta, runtime=docker.PodmanRuntime())
    assert _read_pi_volume_file(destination, "history.jsonl") == b"newer\n"


@pytest.mark.integration
def test_process_death_recovers_and_stops_orphan_writer(legacy_pi_history: SimpleNamespace) -> None:
    meta = legacy_pi_history
    destination = seeded_volumes.volume_name(meta, "pi-sessions")
    real_run = migrations.utils.run

    def abandon_import() -> None:
        def detach_helper(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if cmd[:2] == ["podman", "run"]:
                # Simulate death of the host while its helper is still writing.
                real_run([*cmd[:2], "--detach", *cmd[2:-1], "echo stale > /sessions/stale; exec sleep 300"], **kwargs)
                os._exit(27)
            return real_run(cmd, **kwargs)

        with patch.object(migrations.utils, "run", detach_helper):
            agent.PI.migrate_container_state(meta, runtime=docker.PodmanRuntime())

    child = multiprocessing.get_context("fork").Process(target=abandon_import)
    child.start()
    try:
        child.join(timeout=30)
        assert child.exitcode == 27
        assert _state(meta) == IN_PROGRESS
        helpers = subprocess.run(
            ["podman", "ps", "-a", "-q", "--filter", f"volume={destination}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        assert len(helpers) == 1
        agent.PI.migrate_container_state(meta, runtime=docker.PodmanRuntime())
        assert _state(meta) == COMPLETED
        assert _read_pi_volume_file(destination, "history.jsonl") == b"history"
        subprocess.run(
            ["podman", "run", "--rm", "-v", f"{destination}:/new", PI_TEST_IMAGE, "test", "!", "-e", "/new/stale"],
            check=True,
            capture_output=True,
        )
        assert subprocess.run(["podman", "inspect", helpers[0]], capture_output=True).returncode != 0
    finally:
        if child.is_alive():
            child.terminate()
            child.join(timeout=10)
        child.close()


@pytest.mark.integration
def test_overlapping_hooks_wait_for_migration_commit(
    legacy_pi_history: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meta = legacy_pi_history
    destination = seeded_volumes.volume_name(meta, "pi-sessions")
    copying, release, second_started, second_finished, second_queried = (threading.Event() for _ in range(5))
    copies: list[str] = []
    errors: list[BaseException] = []
    results: list[bytes] = []
    real_run = migrations.utils.run

    def paused_copy(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if threading.current_thread().name == "second" and cmd[:3] == ["podman", "volume", "ls"]:
            second_queried.set()
        if cmd[:2] == ["podman", "run"]:
            copies.append(threading.current_thread().name)
            copying.set()
            if not release.wait(timeout=30):
                raise RuntimeError("test copy timed out")
        return real_run(cmd, **kwargs)

    def launch() -> None:
        try:
            if threading.current_thread().name == "second":
                second_started.set()
            agent.PI.migrate_container_state(meta, runtime=docker.PodmanRuntime())
            results.append(_read_pi_volume_file(destination, "history.jsonl"))
        except BaseException as exc:
            errors.append(exc)
        finally:
            if threading.current_thread().name == "second":
                second_finished.set()

    monkeypatch.setattr(migrations.utils, "run", paused_copy)
    first = threading.Thread(target=launch, name="first")
    second = threading.Thread(target=launch, name="second")
    first.start()
    try:
        assert copying.wait(timeout=20)
        second.start()
        assert second_started.wait(timeout=10)
        assert not second_queried.wait(timeout=0.2)
        assert not second_finished.is_set()
    finally:
        release.set()
        first.join(timeout=30)
        if second.ident is not None:
            second.join(timeout=30)
    assert not first.is_alive() and not second.is_alive()
    assert (copies, results, errors) == (["first"], [b"history", b"history"], [])


def test_migration_query_failure_aborts_hook(
    pi_meta: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    runtime = SimpleNamespace(binary="selected-runtime")

    def unavailable(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(cmd)
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(migrations.utils, "run", unavailable)
    with pytest.raises(subprocess.CalledProcessError):
        agent.PI.migrate_container_state(pi_meta, runtime=runtime)
    assert commands == [["selected-runtime", "volume", "ls", "-q"]]
    assert not pi_meta.session_dir.exists()


@pytest.mark.parametrize("explicit_v1", [False, True])
def test_fresh_upgrade_and_completed_noop(
    pi_meta: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, explicit_v1: bool
) -> None:
    commands: list[list[str]] = []

    def empty_runtime(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(migrations.utils, "run", empty_runtime)
    runtime = docker.PodmanRuntime()
    agent.PI.migrate_container_state(pi_meta, runtime=runtime)
    marker = next((pi_meta.session_dir / "pi-container-state").glob("*.json"))
    if explicit_v1:
        marker.write_text(json.dumps({"layout_version": 1, "migration": None}))
        commands.clear()
        agent.PI.migrate_container_state(pi_meta, runtime=runtime)
    assert (_state(pi_meta), commands) == (COMPLETED, [["podman", "volume", "ls", "-q"]])
    commands.clear()
    agent.PI.migrate_container_state(pi_meta, runtime=runtime)
    assert (_state(pi_meta), commands) == (COMPLETED, [])


@pytest.mark.parametrize(
    "contents,error",
    [
        ('{"layout_version":3,"migration":null}', "Unsupported"),
        ('{"layout_version":0,"migration":null}', "Cannot read"),
        ("not json", "Cannot read"),
        ('{"layout_version":true,"migration":null}', "Cannot read"),
        ('{"layout_version":2}', "Cannot read"),
        ('{"layout_version":1,"migration":{"from_version":2,"to_version":2,"status":"IN_PROGRESS"}}', "Invalid"),
        ('{"layout_version":1,"migration":{"from_version":1,"to_version":3,"status":"IN_PROGRESS"}}', "Invalid"),
        ('{"layout_version":2,"migration":{"from_version":2,"to_version":3,"status":"IN_PROGRESS"}}', "Invalid"),
        ('{"layout_version":1,"migration":{"from_version":1,"to_version":2,"status":"DONE"}}', "Cannot read"),
    ],
)
def test_invalid_state_aborts_without_engine_queries(
    pi_meta: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, contents: str, error: str
) -> None:
    def empty_runtime(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(migrations.utils, "run", empty_runtime)
    agent.PI.migrate_container_state(pi_meta, runtime=docker.PodmanRuntime())
    marker = next((pi_meta.session_dir / "pi-container-state").glob("*.json"))
    marker.write_text(contents)

    def forbidden(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        pytest.fail(f"Unexpected engine query: {cmd}")

    monkeypatch.setattr(migrations.utils, "run", forbidden)
    with pytest.raises(RuntimeError, match=error):
        agent.PI.migrate_container_state(pi_meta, runtime=docker.PodmanRuntime())
    assert marker.read_text() == contents


def test_unversioned_destination_collision_is_not_adopted(
    pi_meta: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = seeded_volumes.volume_name(pi_meta, "pi-sessions")
    commands: list[list[str]] = []

    def existing_destination(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=destination + "\n", stderr="")

    monkeypatch.setattr(migrations.utils, "run", existing_destination)
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        agent.PI.migrate_container_state(pi_meta, runtime=docker.PodmanRuntime())
    assert commands == [["podman", "volume", "ls", "-q"]]
    assert not pi_meta.session_dir.exists()


def test_recovery_query_failure_preserves_progress(pi_meta: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    def empty_runtime(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(migrations.utils, "run", empty_runtime)
    agent.PI.migrate_container_state(pi_meta, runtime=docker.PodmanRuntime())
    marker = next((pi_meta.session_dir / "pi-container-state").glob("*.json"))
    marker.write_text(json.dumps(IN_PROGRESS))
    commands: list[list[str]] = []

    def unavailable(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(cmd)
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(migrations.utils, "run", unavailable)
    with pytest.raises(subprocess.CalledProcessError):
        agent.PI.migrate_container_state(pi_meta, runtime=docker.PodmanRuntime())
    assert (_state(pi_meta), commands) == (IN_PROGRESS, [["podman", "volume", "ls", "-q"]])
