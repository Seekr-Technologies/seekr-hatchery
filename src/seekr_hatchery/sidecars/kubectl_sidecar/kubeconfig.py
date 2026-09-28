"""Kubeconfig generation for the kubectl RBAC proxy."""

from __future__ import annotations

import base64

import yaml

from seekr_hatchery.models import DEFAULT_KUBECTL_CONTEXT_NAME


def make_kubeconfig(contexts: list[tuple[str, int]], proxy_token: str, ca_cert_pem: bytes) -> str:
    """Return a kubeconfig routing each named context through its RBAC proxy.

    The first context is the default. All entries use the same bearer token and
    TLS certificate, but each endpoint independently enforces its own rules.
    """
    if not contexts:
        raise ValueError("make_kubeconfig requires at least one context")

    ca_b64 = base64.b64encode(ca_cert_pem).decode()
    user = "hatchery-agent"
    config = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "name": name,
                "cluster": {
                    "server": f"https://host.docker.internal:{port}",
                    "certificate-authority-data": ca_b64,
                },
            }
            for name, port in contexts
        ],
        "current-context": contexts[0][0],
        "contexts": [{"name": name, "context": {"cluster": name, "user": user}} for name, _ in contexts],
        "users": [{"name": user, "user": {"token": proxy_token}}],
    }
    if len(contexts) == 1 and contexts[0][0] != DEFAULT_KUBECTL_CONTEXT_NAME:
        config["contexts"].append(
            {
                "name": DEFAULT_KUBECTL_CONTEXT_NAME,
                "context": {"cluster": contexts[0][0], "user": user},
            }
        )
    return yaml.safe_dump(config, sort_keys=False)
