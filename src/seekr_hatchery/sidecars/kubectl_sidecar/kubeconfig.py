"""Kubeconfig generation for the kubectl RBAC proxy."""

from __future__ import annotations

import base64
import textwrap


def make_kubeconfig(rbac_port: int, proxy_token: str, ca_cert_pem: bytes) -> str:
    """Return a kubeconfig YAML that routes kubectl through the RBAC proxy over TLS.

    kubectl refuses to send ``Authorization: Bearer`` headers over plain HTTP
    to non-localhost hosts.  This kubeconfig uses ``https://`` and pins the
    self-signed certificate via ``certificate-authority-data``, which is the
    same pattern used by kind / k3d / minikube for local cluster endpoints.

    Args:
        rbac_port: Port where the RBAC proxy is listening (on the host).
        proxy_token: Bearer token embedded for the container to authenticate.
        ca_cert_pem: PEM-encoded self-signed cert returned by
            :func:`seekr_hatchery.sidecars.kubectl_sidecar.rbac_proxy.start_rbac_proxy`.
    """
    ca_b64 = base64.b64encode(ca_cert_pem).decode()
    return textwrap.dedent(f"""\
        apiVersion: v1
        kind: Config
        clusters:
          - name: hatchery-proxy
            cluster:
              server: https://host.docker.internal:{rbac_port}
              certificate-authority-data: {ca_b64}
        current-context: hatchery-proxy
        contexts:
          - name: hatchery-proxy
            context:
              cluster: hatchery-proxy
              user: hatchery-agent
        users:
          - name: hatchery-agent
            user:
              token: {proxy_token}
    """)
