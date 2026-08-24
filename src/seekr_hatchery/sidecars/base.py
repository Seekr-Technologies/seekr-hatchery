"""Sidecar framework: host-side processes whose lifecycle brackets a container launch.

A *sidecar* starts host resource(s) before ``runtime.run`` and tears them down
after, declaring what it contributes to the ``ContainerSpec`` (extra mounts,
extra env, whether the container needs the host gateway) without
``build_spec`` knowing anything about *why*.

Layering (hard constraint): this module sits *below* ``docker.py`` in the
import graph. It has no dependency on any concrete sidecar's transport.
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field

from seekr_hatchery.mount import Mount

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SidecarContribution:
    """What a sidecar contributes to the container being launched.

    The whole (small, transport-free) vocabulary a sidecar has for influencing
    the ``ContainerSpec``: extra mounts, extra env vars, and whether the
    container needs the ``host.docker.internal`` gateway.
    """

    mounts: list[Mount] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    needs_host_gateway: bool = False

    def merge(self, other: "SidecarContribution") -> "SidecarContribution":
        """Combine two contributions: concat mounts, OR the gateway flag.

        Raises ``ValueError`` if both set the same env key — a real collision
        two sidecars cannot silently resolve between themselves.
        """
        conflicts = self.env.keys() & other.env.keys()
        if conflicts:
            raise ValueError(f"sidecar env key conflict: {sorted(conflicts)}")
        return SidecarContribution(
            mounts=[*self.mounts, *other.mounts],
            env={**self.env, **other.env},
            needs_host_gateway=self.needs_host_gateway or other.needs_host_gateway,
        )


class SandboxSidecar(ABC):
    """A host-side process whose lifecycle is tied to a single container launch."""

    name: str

    @abstractmethod
    def start(self) -> SidecarContribution | None:
        """Start host resource(s); return the contribution, or ``None`` if disabled."""

    @abstractmethod
    def stop(self) -> None:
        """Tear down host resource(s).  Idempotent; safe after a failed/partial start."""


@contextmanager
def run_sidecars(sidecars: list[SandboxSidecar]) -> Generator[SidecarContribution, None, None]:
    """Start each sidecar, yield the merged contribution, tear down on exit.

    Teardown is LIFO, partial-start-safe (a sidecar is recorded *before* its
    ``start()`` runs, so a raising start still gets ``stop()``), and isolated
    (one ``stop()`` raising cannot strand another's resources).
    """
    started: list[SandboxSidecar] = []
    merged = SidecarContribution()
    try:
        for sc in sidecars:
            started.append(sc)  # record BEFORE start → a failed start still gets stop()
            contrib = sc.start()
            if contrib is not None:
                merged = merged.merge(contrib)
        yield merged
    finally:
        for sc in reversed(started):  # LIFO teardown
            try:
                sc.stop()
            except Exception:
                logger.exception("error stopping sidecar %s", sc.name)
