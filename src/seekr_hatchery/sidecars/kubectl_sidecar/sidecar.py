"""Lifecycle wrapper around the kubectl proxy chain (see ``.kubectl_proc``, ``.rbac_proxy``, ``.kubeconfig``)."""

from pathlib import Path

import seekr_hatchery.agents as agent
from seekr_hatchery.models import KubectlConfig
from seekr_hatchery.mount import BindMount
from seekr_hatchery.sidecars import base
from seekr_hatchery.sidecars.kubectl_sidecar import kubeconfig, kubectl_proc, rbac_proxy


class KubectlSidecar(base.SandboxSidecar):
    """kubectl proxy subprocess + TLS RBAC-filtering server in front of it.

    Disabled (contributes nothing) when *kube_config* is ``None``.  Otherwise it
    writes a ``0o600`` kubeconfig pointing at the RBAC proxy and contributes it
    as a ``BindMount`` with ``needs_host_gateway=True``.  ``stop()`` reaps the
    RBAC server then the subprocess, each guarded so a partial start still reaps
    whatever did come up.
    """

    name = "kubectl-proxy"

    def __init__(
        self,
        kube_config: KubectlConfig | None,
        session_dir: Path,
        proxy_token: str,
    ) -> None:
        self._config = kube_config
        self._session_dir = session_dir
        self._proxy_token = proxy_token
        self._kubectl_proc: object | None = None
        self._rbac_server: object | None = None

    def start(self) -> base.SidecarContribution | None:
        if self._config is None:
            return None

        self._kubectl_proc, kube_port = kubectl_proc.start_kubectl_proxy_proc(context=self._config.context)
        self._rbac_server, rbac_port, ca_cert_pem = rbac_proxy.start_rbac_proxy(
            self._config.rules, self._proxy_token, kube_port
        )

        kubeconfig_path = self._session_dir / "kubeconfig"
        kubeconfig_path.write_text(kubeconfig.make_kubeconfig(rbac_port, self._proxy_token, ca_cert_pem))
        kubeconfig_path.chmod(0o600)

        return base.SidecarContribution(
            mounts=[BindMount(src=str(kubeconfig_path), dst=f"{agent.CONTAINER_HOME}/.kube/config", mode="RO")],
            needs_host_gateway=True,
        )

    def stop(self) -> None:
        if self._rbac_server is not None:
            rbac_proxy.stop_rbac_proxy(self._rbac_server)
            self._rbac_server = None
        if self._kubectl_proc is not None:
            kubectl_proc.stop_kubectl_proxy_proc(self._kubectl_proc)
            self._kubectl_proc = None
