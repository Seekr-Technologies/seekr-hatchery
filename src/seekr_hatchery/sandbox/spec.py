"""Sandbox spec."""

import sys
from dataclasses import dataclass, field
from pathlib import Path

import seekr_hatchery.sandbox.mount_types as mount_types


@dataclass(frozen=True)
class ContainerSpec:
    """Backend-agnostic description of a container to run.

    The spec carries every flag the old inline assembly assembled —
    but as typed fields rather than ``cmd += [...]`` lines.  Engine-specific
    rendering (userns, label=disable) lives on ``ContainerRuntime`` subclasses.
    """

    image: str
    command: list[str]
    workdir: str
    name: str  # HATCHERY_TASK env value
    container_name: str | None  # --name flag
    mounts: list[mount_types.Mount]
    env: dict[str, str]  # HATCHERY_TASK, HATCHERY_REPO, agent env vars
    cap_add: list[str] = field(default_factory=list)
    cap_drop: list[str] = field(default_factory=list)
    devices: list[str] = field(default_factory=list)
    security_opt: list[str] = field(default_factory=list)
    add_hosts: list[str] = field(default_factory=list)
    interactive: bool = True  # add -it
    rm: bool = True  # add --rm
    init: bool = True  # add --init
    command_override: list[str] | None = None
    capture_output: bool = False  # capture_output for non-interactive override


_SECCOMP = Path(__file__).parent.parent / "resources" / "seccomp.json"


_DIND_CAPS: frozenset[str] = frozenset(
    {
        "SYS_ADMIN",
        "MKNOD",
        "SETUID",
        "SETGID",
        "CHOWN",
        "DAC_OVERRIDE",
        "FOWNER",
        "SETFCAP",
        "SYS_CHROOT",
        "SETPCAP",
        # OCI defaults needed for `podman build` RUN steps (crun capset)
        "AUDIT_WRITE",
        "FSETID",
        "KILL",
        "NET_BIND_SERVICE",
        # Networking caps for bridge networking (k3d, kind, CNI)
        "NET_ADMIN",
        "NET_RAW",
    }
)


def build_spec(
    *,
    image: str,
    mounts: list[mount_types.Mount],
    workdir: str,
    name: str,
    hatchery_repo: str,
    container_name: str | None,
    agent_cmd: list[str],
    user_env: dict[str, str] | None = None,
    extra_env: dict[str, str] | None = None,
    needs_host_gateway: bool = False,
    dind: bool = False,
    cap_add: list[str] | None = None,
    command_override: list[str] | None = None,
    interactive: bool = False,
) -> ContainerSpec:
    """Build a ``ContainerSpec`` from the launch-path inputs.

    This function absorbs the env-var assembly, ``--add-host`` platform gate,
    and DinD cap/device/seccomp assembly that previously lived inline.
    Engine-specific flags (userns, label=disable) are
    *not* here — those live on ``ContainerRuntime.render_run_argv``.

    *user_env* (repo ``environment:`` config) and *extra_env* (sidecar-provided)
    are both merged in after the mandatory ``HATCHERY_*`` vars; *extra_env* is
    applied last so infra values (proxy URLs, tokens) win over user config.

    *needs_host_gateway* emits ``--add-host=host.docker.internal:host-gateway``
    on Linux; the platform gate is applied here as a spec concern.

    *command_override* + *interactive*: when set, the spec runs a raw
    command (e.g. ``/bin/bash``) instead of the agent command.
    *interactive=True* adds ``-it``; *interactive=False* captures output.
    """
    env: dict[str, str] = {
        "HATCHERY_TASK": name,
        "HATCHERY_REPO": hatchery_repo,
    }
    if user_env:
        env.update(user_env)
    if extra_env:
        env.update(extra_env)

    add_hosts: list[str] = []
    if needs_host_gateway and sys.platform == "linux":
        add_hosts.append("host.docker.internal:host-gateway")

    spec_caps_add: list[str] = []
    spec_caps_drop: list[str] = []
    spec_devices: list[str] = []
    spec_security_opt: list[str] = []

    if dind:
        spec_caps_drop = ["ALL"]
        caps = _DIND_CAPS | set(cap_add or [])
        spec_caps_add = sorted(caps)
        spec_devices = ["/dev/fuse"]
        spec_security_opt = ["label=disable", f"seccomp={_SECCOMP}"]

    return ContainerSpec(
        image=image,
        command=agent_cmd,
        workdir=workdir,
        name=name,
        container_name=container_name,
        mounts=mounts,
        env=env,
        cap_add=spec_caps_add,
        cap_drop=spec_caps_drop,
        devices=spec_devices,
        security_opt=spec_security_opt,
        add_hosts=add_hosts,
        interactive=(command_override is None or interactive),
        rm=True,
        init=True,
        command_override=command_override,
        capture_output=not interactive,
    )
