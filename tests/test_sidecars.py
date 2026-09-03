"""Tests for the sidecar framework and its two sidecars."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from seekr_hatchery.agents import CONTAINER_HOME, ProxyEndpoint
from seekr_hatchery.models import KubectlConfig
from seekr_hatchery.mount import BindMount
from seekr_hatchery.sidecars import base
from seekr_hatchery.sidecars.api_sidecar import sidecar as api_sidecar
from seekr_hatchery.sidecars.kubectl_sidecar import kubeconfig, kubectl_proc, rbac_proxy
from seekr_hatchery.sidecars.kubectl_sidecar import sidecar as kubectl_sidecar

# ── SidecarContribution.merge ─────────────────────────────────────────────────


class TestMerge:
    def test_concatenates_mounts_and_ors_gateway(self) -> None:
        a = base.SidecarContribution(mounts=[BindMount(src="/a", dst="/a", mode="RO")])
        b = base.SidecarContribution(
            mounts=[BindMount(src="/b", dst="/b", mode="RW")],
            env={"K": "v"},
            needs_host_gateway=True,
        )
        merged = a.merge(b)
        assert merged == base.SidecarContribution(
            mounts=[BindMount(src="/a", dst="/a", mode="RO"), BindMount(src="/b", dst="/b", mode="RW")],
            env={"K": "v"},
            needs_host_gateway=True,
        )

    def test_raises_on_overlapping_env_keys(self) -> None:
        a = base.SidecarContribution(env={"SHARED": "1"})
        b = base.SidecarContribution(env={"SHARED": "2"})
        with pytest.raises(ValueError, match="SHARED"):
            a.merge(b)


# ── run_sidecars ──────────────────────────────────────────────────────────────


class _RecordingSidecar(base.SandboxSidecar):
    """A test double that logs start/stop into a shared list."""

    def __init__(
        self,
        name: str,
        log: list[tuple[str, str]],
        *,
        contrib: base.SidecarContribution | None = None,
        start_error: Exception | None = None,
        stop_error: Exception | None = None,
    ) -> None:
        self.name = name
        self._log = log
        self._contrib = contrib
        self._start_error = start_error
        self._stop_error = stop_error

    def start(self) -> base.SidecarContribution | None:
        self._log.append(("start", self.name))
        if self._start_error is not None:
            raise self._start_error
        return self._contrib

    def stop(self) -> None:
        self._log.append(("stop", self.name))
        if self._stop_error is not None:
            raise self._stop_error


class TestRunSidecars:
    def test_merges_contributions_and_stops_in_reverse_order(self) -> None:
        log: list[tuple[str, str]] = []
        first = _RecordingSidecar("first", log, contrib=base.SidecarContribution(env={"A": "1"}))
        second = _RecordingSidecar(
            "second", log, contrib=base.SidecarContribution(env={"B": "2"}, needs_host_gateway=True)
        )
        with base.run_sidecars([first, second]) as contrib:
            assert contrib == base.SidecarContribution(env={"A": "1", "B": "2"}, needs_host_gateway=True)
        assert log == [("start", "first"), ("start", "second"), ("stop", "second"), ("stop", "first")]

    def test_disabled_sidecar_contributes_nothing_but_is_still_stopped(self) -> None:
        log: list[tuple[str, str]] = []
        disabled = _RecordingSidecar("disabled", log, contrib=None)
        enabled = _RecordingSidecar("enabled", log, contrib=base.SidecarContribution(env={"A": "1"}))
        with base.run_sidecars([disabled, enabled]) as contrib:
            assert contrib == base.SidecarContribution(env={"A": "1"})
        assert log == [("start", "disabled"), ("start", "enabled"), ("stop", "enabled"), ("stop", "disabled")]

    def test_partial_start_stops_earlier_sidecars_and_propagates(self) -> None:
        log: list[tuple[str, str]] = []
        ok = _RecordingSidecar("ok", log, contrib=base.SidecarContribution())
        boom = _RecordingSidecar("boom", log, start_error=RuntimeError("start failed"))
        never = _RecordingSidecar("never", log, contrib=base.SidecarContribution())
        with pytest.raises(RuntimeError, match="start failed"):
            with base.run_sidecars([ok, boom, never]):
                pytest.fail("body must not run when a start() raises")
        # boom is recorded before its start runs, so it is still stopped; never is untouched.
        assert log == [("start", "ok"), ("start", "boom"), ("stop", "boom"), ("stop", "ok")]

    def test_one_stop_error_does_not_strand_the_other(self) -> None:
        log: list[tuple[str, str]] = []
        first = _RecordingSidecar("first", log, contrib=base.SidecarContribution())
        second = _RecordingSidecar(
            "second", log, contrib=base.SidecarContribution(), stop_error=RuntimeError("stop failed")
        )
        # second stops first (LIFO) and raises; nothing escapes finally and first still stops.
        with base.run_sidecars([first, second]):
            pass
        assert log == [("start", "first"), ("start", "second"), ("stop", "second"), ("stop", "first")]


# ── ApiProxySidecar ─────────────────────────────────────────────────────────


class _FakeBackend:
    def container_env(self, endpoint_key: str, proxy_token: str, proxy_port: int) -> dict[str, str]:
        return {
            f"{endpoint_key.upper()}_API_KEY": proxy_token,
            f"{endpoint_key.upper()}_BASE_URL": f"http://host.docker.internal:{proxy_port}",
        }


class TestApiProxySidecar:
    def test_enabled_builds_env_from_backend_and_requests_gateway(self, monkeypatch) -> None:
        entered: list[str] = []

        @contextmanager
        def fake_api_server(mutator, token, **kwargs):
            entered.append("enter")
            try:
                yield SimpleNamespace(port=4242)
            finally:
                entered.append("exit")

        monkeypatch.setattr(api_sidecar.proxy, "api_server", fake_api_server)
        endpoint = ProxyEndpoint(key="default", header_mutator=lambda h: h, target_host="api.example.com")
        sidecar = api_sidecar.ApiProxySidecar(endpoint, "proxy-token", _FakeBackend())
        contrib = sidecar.start()
        assert contrib == base.SidecarContribution(
            env={"DEFAULT_API_KEY": "proxy-token", "DEFAULT_BASE_URL": "http://host.docker.internal:4242"},
            needs_host_gateway=True,
        )
        assert entered == ["enter"]
        sidecar.stop()
        assert entered == ["enter", "exit"]

    def test_two_endpoints_merge_into_one_contribution_without_collision(self, monkeypatch) -> None:
        ports = iter([4242, 5353])

        @contextmanager
        def fake_api_server(mutator, token, **kwargs):
            yield SimpleNamespace(port=next(ports))

        monkeypatch.setattr(api_sidecar.proxy, "api_server", fake_api_server)
        backend = _FakeBackend()
        endpoints = [
            ProxyEndpoint(key="acme", header_mutator=lambda h: h, target_host="api.acme.com"),
            ProxyEndpoint(key="openai", header_mutator=lambda h: h, target_host="api.openai.com"),
        ]
        sidecars_list = [api_sidecar.ApiProxySidecar(ep, "tok", backend) for ep in endpoints]
        with base.run_sidecars(sidecars_list) as contrib:
            assert contrib == base.SidecarContribution(
                env={
                    "ACME_API_KEY": "tok",
                    "ACME_BASE_URL": "http://host.docker.internal:4242",
                    "OPENAI_API_KEY": "tok",
                    "OPENAI_BASE_URL": "http://host.docker.internal:5353",
                },
                needs_host_gateway=True,
            )

    def test_colliding_env_keys_across_endpoints_raise(self, monkeypatch) -> None:
        @contextmanager
        def fake_api_server(mutator, token, **kwargs):
            yield SimpleNamespace(port=4242)

        monkeypatch.setattr(api_sidecar.proxy, "api_server", fake_api_server)

        class _CollidingBackend:
            def container_env(self, endpoint_key: str, proxy_token: str, proxy_port: int) -> dict[str, str]:
                return {"SHARED_KEY": proxy_token}

        backend = _CollidingBackend()
        endpoints = [
            ProxyEndpoint(key="a", header_mutator=lambda h: h, target_host="api.a.example"),
            ProxyEndpoint(key="b", header_mutator=lambda h: h, target_host="api.b.example"),
        ]
        sidecars_list = [api_sidecar.ApiProxySidecar(ep, "tok", backend) for ep in endpoints]
        with pytest.raises(ValueError, match="SHARED_KEY"):
            with base.run_sidecars(sidecars_list):
                pytest.fail("body must not run when contributions collide")


# ── KubectlSidecar ──────────────────────────────────────────────────────────


class TestKubectlSidecar:
    def test_disabled_when_config_is_none(self, tmp_path: Path) -> None:
        sidecar = kubectl_sidecar.KubectlSidecar(None, tmp_path, "tok")
        assert sidecar.start() is None
        sidecar.stop()  # safe after a no-op start

    def _patch_kubectl(self, monkeypatch, log: list[str], *, rbac_error: bool = False):
        proc = SimpleNamespace(name="proc")
        rbac = SimpleNamespace(name="rbac")

        def start_proc(context=None):
            log.append(f"start_proc:{context}")
            return proc, 8001

        def start_rbac(rules, token, kube_port):
            log.append("start_rbac")
            if rbac_error:
                raise RuntimeError("rbac boom")
            return rbac, 8443, b"cert-pem"

        monkeypatch.setattr(kubectl_proc, "start_kubectl_proxy_proc", start_proc)
        monkeypatch.setattr(rbac_proxy, "start_rbac_proxy", start_rbac)
        monkeypatch.setattr(kubeconfig, "make_kubeconfig", lambda *a: "kubeconfig-yaml")
        monkeypatch.setattr(rbac_proxy, "stop_rbac_proxy", lambda server: log.append(f"stop_rbac:{server.name}"))
        monkeypatch.setattr(kubectl_proc, "stop_kubectl_proxy_proc", lambda p: log.append(f"stop_proc:{p.name}"))
        return proc, rbac

    def test_enabled_writes_0600_kubeconfig_and_one_bind_mount(self, tmp_path: Path, monkeypatch) -> None:
        log: list[str] = []
        self._patch_kubectl(monkeypatch, log)
        sidecar = kubectl_sidecar.KubectlSidecar(KubectlConfig(context="my-ctx"), tmp_path, "tok")
        contrib = sidecar.start()

        kubeconfig_path = tmp_path / "kubeconfig"
        assert contrib == base.SidecarContribution(
            mounts=[BindMount(src=str(kubeconfig_path), dst=f"{CONTAINER_HOME}/.kube/config", mode="RO")],
            needs_host_gateway=True,
        )
        assert kubeconfig_path.read_text() == "kubeconfig-yaml"
        assert oct(kubeconfig_path.stat().st_mode & 0o777) == "0o600"
        assert log == ["start_proc:my-ctx", "start_rbac"]

    def test_stop_reaps_rbac_then_proc(self, tmp_path: Path, monkeypatch) -> None:
        log: list[str] = []
        self._patch_kubectl(monkeypatch, log)
        sidecar = kubectl_sidecar.KubectlSidecar(KubectlConfig(), tmp_path, "tok")
        sidecar.start()
        log.clear()
        sidecar.stop()
        assert log == ["stop_rbac:rbac", "stop_proc:proc"]

    def test_partial_start_still_reaps_proc(self, tmp_path: Path, monkeypatch) -> None:
        log: list[str] = []
        self._patch_kubectl(monkeypatch, log, rbac_error=True)
        sidecar = kubectl_sidecar.KubectlSidecar(KubectlConfig(), tmp_path, "tok")
        with pytest.raises(RuntimeError, match="rbac boom"):
            sidecar.start()
        log.clear()
        sidecar.stop()
        # RBAC server never came up, but the kubectl proc did — so only it is reaped.
        assert log == ["stop_proc:proc"]
