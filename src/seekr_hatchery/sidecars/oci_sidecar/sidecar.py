"""Lifecycle wrapper for the host-side OCI Object Storage proxy."""

from pathlib import Path

from seekr_hatchery.agents import CONTAINER_HOME
from seekr_hatchery.mount import BindMount
from seekr_hatchery.sidecars import base
from seekr_hatchery.sidecars.oci_sidecar import credentials, proxy
from seekr_hatchery.sidecars.oci_sidecar.config import OciConfig


class OciSidecar(base.SandboxSidecar):
    """Expose host OCI API-key profiles without mounting their credentials."""

    name = "oci"

    def __init__(self, config: OciConfig | None, session_dir: Path) -> None:
        self._config = config
        self._session_dir = session_dir
        self._cm: object | None = None

    def validate(self) -> None:
        """Verify configured host API-key profiles before the image is built."""
        if self._config is not None:
            credentials.validate_host_profiles_exist(self._config)

    def start(self) -> base.SidecarContribution | None:
        if self._config is None:
            return None
        resolved = {
            profile_name: (
                credentials.resolve_oci_profile(self._config, profile_name, profile),
                profile.rules,
            )
            for profile_name, profile in self._config.profiles.items()
        }
        self._cm = proxy.oci_server(resolved)
        server = self._cm.__enter__()
        client_dir = server.write_client_files(self._session_dir / "oci")
        return base.SidecarContribution(
            mounts=[BindMount(src=client_dir, dst=f"{CONTAINER_HOME}/.oci", mode="RO")],
            env=server.container_env(self._config.default_profile),
            needs_host_gateway=True,
        )

    def stop(self) -> None:
        if self._cm is not None:
            self._cm.__exit__(None, None, None)
            self._cm = None
