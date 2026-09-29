"""Composition registry for sandbox capability providers.

Providers own the sidecars required for one sandbox capability.  The registry
is deliberately small: built-ins register directly and this module is the
extraction seam for a future trusted Python entry-point integration.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import seekr_hatchery.agents as agent
from seekr_hatchery.models import KubectlConfig
from seekr_hatchery.sidecars.api_sidecar import ApiProxySidecar
from seekr_hatchery.sidecars.base import SandboxSidecar
from seekr_hatchery.sidecars.kubectl_sidecar import KubectlSidecar


@dataclass(frozen=True)
class SessionProviderContext:
    """Inputs providers may need while launching an agent session."""

    backend: agent.AgentBackend
    endpoints: list[agent.ProxyEndpoint]
    proxy_token: str
    kubernetes: KubectlConfig | None
    session_dir: Path
    kubectl_proxy_token: str


@dataclass(frozen=True)
class ShellProviderContext:
    """Inputs providers may need while launching ``hatchery sandbox shell``."""

    kubernetes: KubectlConfig | None
    session_dir: Path
    kubectl_proxy_token: str


class SandboxProvider(Protocol):
    """A built-in or trusted-host-code provider of sandbox sidecars."""

    name: str

    def session_sidecars(self, context: SessionProviderContext) -> list[SandboxSidecar]:
        """Return sidecars for an agent session."""

    def shell_sidecars(self, context: ShellProviderContext) -> list[SandboxSidecar]:
        """Return sidecars for an interactive sandbox shell."""


class SandboxProviderRegistry:
    """Ordered registry that composes sandbox providers without docker coupling."""

    def __init__(self) -> None:
        self._providers: list[SandboxProvider] = []

    def register(self, provider: SandboxProvider) -> None:
        """Register *provider*, rejecting duplicate names deterministically."""
        if any(existing.name == provider.name for existing in self._providers):
            raise ValueError(f"sandbox provider already registered: {provider.name}")
        self._providers.append(provider)

    def session_sidecars(self, context: SessionProviderContext) -> list[SandboxSidecar]:
        """Return the ordered sidecars contributed to an agent session."""
        return [sidecar for provider in self._providers for sidecar in provider.session_sidecars(context)]

    def shell_sidecars(self, context: ShellProviderContext) -> list[SandboxSidecar]:
        """Return the ordered sidecars contributed to a sandbox shell."""
        return [sidecar for provider in self._providers for sidecar in provider.shell_sidecars(context)]


class _ApiProxyProvider:
    name = "api-proxy"

    def session_sidecars(self, context: SessionProviderContext) -> list[SandboxSidecar]:
        return [ApiProxySidecar(endpoint, context.proxy_token, context.backend) for endpoint in context.endpoints]

    def shell_sidecars(self, context: ShellProviderContext) -> list[SandboxSidecar]:
        return []


class _KubernetesProvider:
    name = "kubernetes"

    def session_sidecars(self, context: SessionProviderContext) -> list[SandboxSidecar]:
        return [KubectlSidecar(context.kubernetes, context.session_dir, context.kubectl_proxy_token)]

    def shell_sidecars(self, context: ShellProviderContext) -> list[SandboxSidecar]:
        return [KubectlSidecar(context.kubernetes, context.session_dir, context.kubectl_proxy_token)]


builtin_providers = SandboxProviderRegistry()
builtin_providers.register(_ApiProxyProvider())
builtin_providers.register(_KubernetesProvider())
