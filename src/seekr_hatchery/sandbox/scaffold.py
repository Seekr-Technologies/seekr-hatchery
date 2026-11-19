"""Sandbox scaffold."""

from pathlib import Path

import seekr_hatchery.agents as agent
import seekr_hatchery.constants as constants
import seekr_hatchery.ui as ui
import seekr_hatchery.utils as utils

_RESOURCES = Path(__file__).parent.parent / "resources"


_DOCKERFILE_TEMPLATE = _RESOURCES / "Dockerfile.template"


_DOCKER_CONFIG_TEMPLATE = _RESOURCES / "docker.yaml.template"


_LOCAL_DOCKER_CONFIG_TEMPLATE = _RESOURCES / "docker.local.yaml.template"


DIND_DOCKERFILE_LINES = """\
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \\
    podman podman-docker sudo fuse-overlayfs uidmap \\
    && rm -rf /var/lib/apt/lists/*
RUN printf 'hatchery:1001:64535\\n' > /etc/subuid \\
    && printf 'hatchery:1001:64535\\n' > /etc/subgid
RUN mkdir -p /root/.config/containers \\
    && printf '[storage]\\ndriver = "overlay"\\n\\n[storage.options.overlay]\\nmount_program = "/usr/bin/fuse-overlayfs"\\n' \\
        > /root/.config/containers/storage.conf \\
    && printf '[containers]\\nnetns = "host"\\ncgroups = "disabled"\\ndefault_sysctls = []\\n[engine]\\ncgroup_manager = "cgroupfs"\\nevents_logger = "file"\\n' \\
        > /root/.config/containers/containers.conf
RUN echo 'hatchery ALL=(root) NOPASSWD: /usr/bin/podman' \\
        > /etc/sudoers.d/hatchery-podman \\
    && chmod 440 /etc/sudoers.d/hatchery-podman
RUN printf '#!/bin/sh\\nexec sudo -n /usr/bin/podman "$@"\\n' \\
        > /usr/local/bin/podman \\
    && chmod +x /usr/local/bin/podman
USER hatchery"""


def _comment_out(text: str) -> str:
    """Prefix every line of *text* with ``# ``."""
    return "\n".join(f"# {line}" for line in text.splitlines())


def dockerfile_path(hatchery_dir: Path, backend: agent.AgentBackend) -> Path:
    """Return the canonical path to this backend's Dockerfile under *hatchery_dir*.

    Files are named ``Dockerfile.<agent>`` (e.g. ``Dockerfile.codex``).
    """
    return hatchery_dir / f"Dockerfile.{backend.kind.lower()}"


def _pin_harness_latest(text: str, backend: agent.AgentBackend) -> str:
    """Pin the backend's harness to the registry's latest version in *text*.

    Returns *text* unchanged if the backend has no pinnable harness (e.g. one
    that installs at build time), or if the version lookup fails — in that case
    the harness is left unpinned and the build pulls latest itself.
    """
    try:
        result = backend.update(text)
    except (OSError, ValueError, KeyError) as e:
        ui.warn(f"  Couldn't fetch latest {backend.kind.lower()} version ({e}); leaving harness unpinned.")
        return text
    return text if result is None else result[0]


def ensure_dockerfile(
    hatchery_dir: Path,
    backend: agent.AgentBackend = agent.CODEX,
    *,
    prompt: bool = True,
) -> bool:
    """Write a starter Dockerfile if none exists. Returns True if created.

    When *prompt* is False the file is written silently and the caller is
    responsible for opening it (used when an editor drives the flow itself).
    """
    df = dockerfile_path(hatchery_dir, backend)
    if df.exists():
        return False
    df.parent.mkdir(parents=True, exist_ok=True)
    text = _DOCKERFILE_TEMPLATE.read_text()
    text = text.replace("{{AGENT_INSTALL}}", backend.dockerfile_install)
    text = text.replace("{{DIND}}", _comment_out(DIND_DOCKERFILE_LINES))
    text = _pin_harness_latest(text, backend)
    df.write_text(text)
    ui.info(f"  Created {df.relative_to(hatchery_dir)}")
    if prompt:
        answer = input("  Would you like to edit the Dockerfile? [Y/n] ").strip().lower()
        if answer != "n":
            utils.open_for_editing(df)
    return True


def ensure_config(hatchery_dir: Path, *, prompt: bool = True) -> bool:
    """Write docker.yaml from template if it does not already exist.

    Returns True if the file was created, False if it already existed. When
    *prompt* is False the file is written silently and the caller is responsible
    for opening it (used when an editor drives the flow itself).
    """
    config_file = hatchery_dir / constants.DOCKER_CONFIG
    if config_file.exists():
        return False
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(_DOCKER_CONFIG_TEMPLATE.read_text())
    ui.info(f"  Created {constants.DOCKER_CONFIG}")
    if prompt:
        answer = input("  Would you like to edit the docker config? [Y/n] ").strip().lower()
        if answer != "n":
            utils.open_for_editing(config_file)
    return True


def ensure_local_config(hatchery_dir: Path) -> bool:
    """Write an uncommitted local overlay template when it does not exist."""
    config_file = hatchery_dir / constants.LOCAL_DOCKER_CONFIG
    if config_file.exists():
        return False
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(_LOCAL_DOCKER_CONFIG_TEMPLATE.read_text())
    ui.info(f"  Created {constants.LOCAL_DOCKER_CONFIG}")
    return True


def supports_dind(hatchery_dir: Path, backend: agent.AgentBackend) -> bool:
    """Return True if the Dockerfile has an uncommented fuse-overlayfs reference.

    fuse-overlayfs is the storage driver required for rootless nested containers
    and is a reliable smoke-test that the DinD section has been uncommented.
    """
    df = dockerfile_path(hatchery_dir, backend)
    if not df.exists():
        return False
    for line in df.read_text().splitlines():
        stripped = line.strip()
        if not stripped.startswith("#") and "fuse-overlayfs" in stripped:
            return True
    return False
