"""Lifecycle wrapper around kubectl proxy and RBAC proxy pairs."""

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

import seekr_hatchery.agents as agent
import seekr_hatchery.ui as ui
from seekr_hatchery.models import KubectlConfig
from seekr_hatchery.mount import BindMount
from seekr_hatchery.sidecars import base
from seekr_hatchery.sidecars.kubectl_sidecar import kubeconfig, kubectl_proc, rbac_proxy

logger = logging.getLogger(__name__)


@dataclass
class _ContextProxy:
    """One running kubectl and RBAC proxy pair."""

    name: str
    kubectl_proc: object
    rbac_server: object
    rbac_port: int


class KubectlSidecar(base.SandboxSidecar):
    """Expose configured Kubernetes contexts through independently-filtered proxies.

    Each healthy configured context gets its own host-side kubectl and RBAC
    proxy pair. Failures are reported and skipped so stale credentials for one
    cluster do not prevent access to another; an all-failed startup raises.
    """

    name = "kubectl-proxy"

    def __init__(
        self,
        kube_config: KubectlConfig | None,
        session_dir: Path,
        proxy_token: str | None,
    ) -> None:
        self._config = kube_config
        self._session_dir = session_dir
        self._proxy_token = proxy_token or (str(uuid.uuid4()) if kube_config is not None else "")
        self._proxies: list[_ContextProxy] = []

    def start(self) -> base.SidecarContribution | None:
        if self._config is None:
            return None

        certificate = rbac_proxy._generate_self_signed_cert()
        for context in self._config.resolved_contexts():
            proc: object | None = None
            try:
                proc, kube_port = kubectl_proc.start_kubectl_proxy_proc(context=context.context)
                server, rbac_port, _ = rbac_proxy.start_rbac_proxy(
                    context.rules,
                    self._proxy_token,
                    kube_port,
                    certificate=certificate,
                )
            except Exception as exc:
                if proc is not None:
                    kubectl_proc.stop_kubectl_proxy_proc(proc)
                ui.warn(f"kubectl context {context.display_name!r} failed to start: {exc} — continuing without it")
                continue
            self._proxies.append(_ContextProxy(context.display_name, proc, server, rbac_port))

        if not self._proxies:
            raise RuntimeError("kubectl: no configured context could be started")

        kubeconfig_path = self._session_dir / "kubeconfig"
        kubeconfig_path.write_text(
            kubeconfig.make_kubeconfig(
                [(proxy.name, proxy.rbac_port) for proxy in self._proxies], self._proxy_token, certificate[0]
            )
        )
        kubeconfig_path.chmod(0o600)
        return base.SidecarContribution(
            mounts=[BindMount(src=str(kubeconfig_path), dst=f"{agent.CONTAINER_HOME}/.kube/config", mode="RO")],
            needs_host_gateway=True,
        )

    def stop(self) -> None:
        for proxy in reversed(self._proxies):
            try:
                rbac_proxy.stop_rbac_proxy(proxy.rbac_server)
            except Exception:
                logger.exception("failed to stop RBAC proxy for kubectl context %r", proxy.name)
            try:
                kubectl_proc.stop_kubectl_proxy_proc(proxy.kubectl_proc)
            except Exception:
                logger.exception("failed to stop kubectl proxy for context %r", proxy.name)
        self._proxies.clear()
