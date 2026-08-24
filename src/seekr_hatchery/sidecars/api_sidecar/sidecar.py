"""Lifecycle wrapper around the API reverse proxy (see ``.proxy``)."""

import sys
from collections.abc import Callable

import seekr_hatchery.agents as agent
import seekr_hatchery.ui as ui
from seekr_hatchery.sidecars import base
from seekr_hatchery.sidecars.api_sidecar import proxy


class ApiProxySidecar(base.SandboxSidecar):
    """Credentialed reverse proxy in front of the agent's upstream API.

    Disabled (contributes nothing) when *mutator* is ``None`` — e.g. sandbox
    shell sessions that run no agent.  Otherwise it owns the
    ``backend.container_env(token, port)`` computation that ``build_spec`` used
    to do, returning it as ``contribution.env`` with ``needs_host_gateway=True``.
    """

    name = "api-proxy"

    def __init__(
        self,
        mutator: Callable[..., dict[str, str]] | None,
        proxy_token: str | None,
        backend: agent.AgentBackend,
    ) -> None:
        self._mutator = mutator
        self._proxy_token = proxy_token
        self._backend = backend
        self._cm: object | None = None

    def start(self) -> base.SidecarContribution | None:
        if self._mutator is None:
            return None
        try:
            kwargs = self._backend.proxy_kwargs()
        except RuntimeError as exc:
            ui.error(str(exc))
            sys.exit(1)
        self._cm = proxy.api_server(self._mutator, self._proxy_token or "", **kwargs)
        server = self._cm.__enter__()
        env = self._backend.container_env(self._proxy_token or "", server.port)
        return base.SidecarContribution(env=dict(env), needs_host_gateway=True)

    def stop(self) -> None:
        if self._cm is not None:
            self._cm.__exit__(None, None, None)
            self._cm = None
