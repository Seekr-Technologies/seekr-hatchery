"""Lifecycle wrapper for the host-side S3 SigV4 proxy."""

from pathlib import Path

from seekr_hatchery.agents import CONTAINER_HOME
from seekr_hatchery.mount import BindMount
from seekr_hatchery.sidecars import base
from seekr_hatchery.sidecars.s3_sidecar import credentials, proxy
from seekr_hatchery.sidecars.s3_sidecar.config import S3Config


class S3Sidecar(base.SandboxSidecar):
    """Expose a configured host AWS profile without mounting its credentials."""

    name = "s3"

    def __init__(self, config: S3Config | None, session_dir: Path) -> None:
        self._config = config
        self._session_dir = session_dir
        self._cm: object | None = None

    def validate(self) -> None:
        """Verify configured profiles exist in the host AWS config before build."""
        if self._config is not None:
            credentials.validate_host_profiles_exist(self._config)

    def start(self) -> base.SidecarContribution | None:
        if self._config is None:
            return None
        resolved = {
            alias: (credentials.resolve_aws_profile(alias, profile), profile.rules)
            for alias, profile in self._config.profiles.items()
        }
        self._cm = proxy.s3_server(resolved)
        server = self._cm.__enter__()
        client_dir = server.write_client_files(self._session_dir / "aws")
        return base.SidecarContribution(
            mounts=[BindMount(src=client_dir, dst=f"{CONTAINER_HOME}/.aws", mode="RO")],
            env=server.container_env(self._config.default_alias),
            needs_host_gateway=True,
        )

    def stop(self) -> None:
        if self._cm is not None:
            self._cm.__exit__(None, None, None)
            self._cm = None
