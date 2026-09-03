"""Lifecycle wrapper around the API reverse proxy (see ``.proxy``)."""

import seekr_hatchery.agents as agent
from seekr_hatchery.sidecars import base
from seekr_hatchery.sidecars.api_sidecar import proxy


class ApiProxySidecar(base.SandboxSidecar):
    """Credentialed reverse proxy in front of one of the agent's upstream APIs.

    One instance per ``ProxyEndpoint``. Owns the
    ``backend.container_env(endpoint.key, token, port)`` computation that
    ``build_spec`` used to do, returning it as ``contribution.env`` with
    ``needs_host_gateway=True``. Callers that want no proxy (e.g. sandbox
    shell sessions that run no agent) simply don't construct this sidecar.
    """

    name = "api-proxy"

    def __init__(
        self,
        endpoint: agent.ProxyEndpoint,
        proxy_token: str | None,
        backend: agent.AgentBackend,
    ) -> None:
        self._endpoint = endpoint
        self._proxy_token = proxy_token
        self._backend = backend
        self._cm: object | None = None
        self.name = f"api-proxy:{endpoint.key}"

    def start(self) -> base.SidecarContribution | None:
        # The endpoint may override the fake token the container presents (and
        # this proxy validates) — e.g. pi's openai-codex JWT carrying the real
        # account id. Both the proxy check and the container env must use it.
        token = self._endpoint.container_token or self._proxy_token or ""
        self._cm = proxy.api_server(
            self._endpoint.header_mutator,
            token,
            target_host=self._endpoint.target_host,
            path_prefix=self._endpoint.path_prefix,
        )
        server = self._cm.__enter__()
        env = self._backend.container_env(self._endpoint.key, token, server.port)
        return base.SidecarContribution(env=dict(env), needs_host_gateway=True)

    def stop(self) -> None:
        if self._cm is not None:
            self._cm.__exit__(None, None, None)
            self._cm = None
