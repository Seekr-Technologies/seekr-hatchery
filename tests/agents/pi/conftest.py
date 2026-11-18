"""Shared Pi task and live-runtime fixtures."""

import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

PI_TEST_IMAGE = "docker.io/library/alpine:3.21"


@pytest.fixture
def pi_meta(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        container_name=f"pi-test-{uuid.uuid4().hex}",
        image_name=PI_TEST_IMAGE,
        session_dir=tmp_path / "session",
    )


@pytest.fixture
def pi_container(pi_meta: SimpleNamespace) -> SimpleNamespace:
    if shutil.which("podman") is None or subprocess.run(["podman", "info"], capture_output=True).returncode:
        pytest.skip("podman unavailable")
    if subprocess.run(["podman", "image", "exists", PI_TEST_IMAGE], capture_output=True).returncode:
        subprocess.run(["podman", "pull", "-q", PI_TEST_IMAGE], check=True, capture_output=True)
    return pi_meta
