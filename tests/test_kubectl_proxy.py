"""Tests for the kubectl RBAC proxy module."""

from __future__ import annotations

import http.client
import http.server
import json
import ssl
import tempfile
import threading

import pytest

from seekr_hatchery.kubectl_proxy import (
    KubectlConfig,
    KubectlRBACRule,
    check_rbac,
    http_method_to_k8s_verbs,
    make_kubeconfig,
    parse_k8s_url,
    start_rbac_proxy,
    stop_rbac_proxy,
)

# ── URL parsing ───────────────────────────────────────────────────────────────


class TestParseK8sUrl:
    def test_core_namespaced_collection(self) -> None:
        assert parse_k8s_url("/api/v1/namespaces/default/pods") == ("default", "pods", "")

    def test_core_namespaced_named_resource(self) -> None:
        assert parse_k8s_url("/api/v1/namespaces/staging/pods/my-pod") == ("staging", "pods", "")

    def test_core_namespaced_subresource(self) -> None:
        assert parse_k8s_url("/api/v1/namespaces/default/pods/my-pod/exec") == ("default", "pods", "exec")

    def test_core_namespaced_log_subresource(self) -> None:
        assert parse_k8s_url("/api/v1/namespaces/default/pods/my-pod/log") == ("default", "pods", "log")

    def test_core_cluster_scoped(self) -> None:
        assert parse_k8s_url("/api/v1/nodes") == ("", "nodes", "")

    def test_core_cluster_scoped_named(self) -> None:
        assert parse_k8s_url("/api/v1/nodes/my-node") == ("", "nodes", "")

    def test_group_namespaced_collection(self) -> None:
        assert parse_k8s_url("/apis/apps/v1/namespaces/default/deployments") == ("default", "deployments", "")

    def test_group_namespaced_named(self) -> None:
        assert parse_k8s_url("/apis/apps/v1/namespaces/staging/deployments/my-dep") == ("staging", "deployments", "")

    def test_group_cluster_scoped(self) -> None:
        assert parse_k8s_url("/apis/apps/v1/deployments") == ("", "deployments", "")

    def test_discovery_api(self) -> None:
        assert parse_k8s_url("/api") == ("", "", "")

    def test_discovery_apis(self) -> None:
        assert parse_k8s_url("/apis") == ("", "", "")

    def test_healthz(self) -> None:
        assert parse_k8s_url("/healthz") == ("", "", "")

    def test_version(self) -> None:
        assert parse_k8s_url("/version") == ("", "", "")

    def test_query_string_stripped(self) -> None:
        assert parse_k8s_url("/api/v1/namespaces/default/pods?watch=true") == ("default", "pods", "")

    def test_trailing_slash_stripped(self) -> None:
        assert parse_k8s_url("/api/v1/namespaces/default/pods/") == ("default", "pods", "")

    def test_portforward_subresource(self) -> None:
        assert parse_k8s_url("/api/v1/namespaces/default/pods/my-pod/portforward") == ("default", "pods", "portforward")


# ── HTTP verb mapping ─────────────────────────────────────────────────────────


class TestHttpMethodToK8sVerbs:
    def test_get(self) -> None:
        assert http_method_to_k8s_verbs("GET") == ["get", "list", "watch"]

    def test_post(self) -> None:
        assert http_method_to_k8s_verbs("POST") == ["create"]

    def test_put(self) -> None:
        assert http_method_to_k8s_verbs("PUT") == ["update"]

    def test_patch(self) -> None:
        assert http_method_to_k8s_verbs("PATCH") == ["patch"]

    def test_delete(self) -> None:
        assert http_method_to_k8s_verbs("DELETE") == ["delete", "deletecollection"]

    def test_case_insensitive(self) -> None:
        assert http_method_to_k8s_verbs("get") == ["get", "list", "watch"]


# ── RBAC checking ─────────────────────────────────────────────────────────────


class TestCheckRbac:
    def test_empty_rules_denies_everything(self) -> None:
        assert check_rbac([], ["get", "list", "watch"], "pods", "default") is False

    def test_wildcard_verbs_and_resources_and_namespaces(self) -> None:
        rules = [KubectlRBACRule(verbs=["*"], resources=["*"], namespaces=["*"])]
        assert check_rbac(rules, ["delete"], "pods", "production") is True

    def test_allow_matching_verb(self) -> None:
        rules = [KubectlRBACRule(verbs=["get", "list"], resources=["pods"], namespaces=["default"])]
        assert check_rbac(rules, ["get", "list", "watch"], "pods", "default") is True

    def test_deny_wrong_verb(self) -> None:
        rules = [KubectlRBACRule(verbs=["get", "list"], resources=["pods"], namespaces=["default"])]
        assert check_rbac(rules, ["delete"], "pods", "default") is False

    def test_deny_wrong_resource(self) -> None:
        rules = [KubectlRBACRule(verbs=["get"], resources=["pods"], namespaces=["*"])]
        assert check_rbac(rules, ["get", "list", "watch"], "secrets", "default") is False

    def test_deny_wrong_namespace(self) -> None:
        rules = [KubectlRBACRule(verbs=["get"], resources=["pods"], namespaces=["default"])]
        assert check_rbac(rules, ["get", "list", "watch"], "pods", "production") is False

    def test_wildcard_namespace_matches_cluster_scoped(self) -> None:
        """namespaces: ['*'] should match cluster-scoped requests (namespace='')."""
        rules = [KubectlRBACRule(verbs=["get"], resources=["pods"], namespaces=["*"])]
        assert check_rbac(rules, ["get", "list", "watch"], "pods", "") is True

    def test_specific_namespace_does_not_match_cluster_scoped(self) -> None:
        """namespaces: ['default'] should NOT match cluster-scoped queries (namespace='')."""
        rules = [KubectlRBACRule(verbs=["get"], resources=["pods"], namespaces=["default"])]
        assert check_rbac(rules, ["get", "list", "watch"], "pods", "") is False

    def test_empty_string_namespace_allows_cluster_scoped(self) -> None:
        """namespaces: [''] explicitly allows cluster-scoped requests."""
        rules = [KubectlRBACRule(verbs=["get"], resources=["pods"], namespaces=[""])]
        assert check_rbac(rules, ["get", "list", "watch"], "pods", "") is True

    def test_multiple_rules_first_match_wins(self) -> None:
        rules = [
            KubectlRBACRule(verbs=["get"], resources=["pods"], namespaces=["default"]),
            KubectlRBACRule(verbs=["delete"], resources=["pods"], namespaces=["default"]),
        ]
        assert check_rbac(rules, ["delete"], "pods", "default") is True

    def test_wildcard_resource(self) -> None:
        rules = [KubectlRBACRule(verbs=["get"], resources=["*"], namespaces=["*"])]
        assert check_rbac(rules, ["get", "list", "watch"], "secrets", "kube-system") is True


# ── KubectlConfig model ───────────────────────────────────────────────────────


class TestKubectlConfig:
    def test_default_empty_rules(self) -> None:
        cfg = KubectlConfig()
        assert cfg.rules == []

    def test_default_context_is_none(self) -> None:
        cfg = KubectlConfig()
        assert cfg.context is None

    def test_context_field(self) -> None:
        cfg = KubectlConfig(context="my-dev-cluster", rules=[])
        assert cfg.context == "my-dev-cluster"

    def test_parse_from_dict(self) -> None:
        cfg = KubectlConfig(
            rules=[
                {"verbs": ["get", "list"], "resources": ["pods"], "namespaces": ["default"]},
            ]
        )
        assert len(cfg.rules) == 1
        assert cfg.rules[0].verbs == ["get", "list"]

    def test_default_namespaces_is_wildcard(self) -> None:
        rule = KubectlRBACRule(verbs=["get"], resources=["pods"])
        assert rule.namespaces == ["*"]

    def test_unknown_verb_emits_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """'describe' and other client-side commands are not real k8s verbs."""
        import logging

        with caplog.at_level(logging.WARNING, logger="hatchery"):
            KubectlRBACRule(verbs=["get", "describe"], resources=["pods"])
        assert any("describe" in r.message for r in caplog.records)

    def test_known_verbs_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        with caplog.at_level(logging.WARNING, logger="hatchery"):
            KubectlRBACRule(verbs=["get", "list", "watch", "create", "update", "patch", "delete"], resources=["pods"])
        assert not any("unrecognized" in r.message for r in caplog.records)


# ── make_kubeconfig ───────────────────────────────────────────────────────────


_DUMMY_CERT = b"-----BEGIN CERTIFICATE-----\nZmFrZWNlcnQ=\n-----END CERTIFICATE-----\n"


class TestMakeKubeconfig:
    def test_contains_rbac_port(self) -> None:
        kc = make_kubeconfig(12345, "my-token", _DUMMY_CERT)
        assert "12345" in kc

    def test_contains_token(self) -> None:
        kc = make_kubeconfig(12345, "my-secret-token", _DUMMY_CERT)
        assert "my-secret-token" in kc

    def test_valid_yaml(self) -> None:
        import yaml

        kc = make_kubeconfig(8080, "tok", _DUMMY_CERT)
        parsed = yaml.safe_load(kc)
        assert parsed["kind"] == "Config"
        assert parsed["current-context"] == "hatchery-proxy"

    def test_uses_https(self) -> None:
        kc = make_kubeconfig(8080, "tok", _DUMMY_CERT)
        assert "https://" in kc

    def test_embeds_ca_cert(self) -> None:
        import base64

        kc = make_kubeconfig(8080, "tok", _DUMMY_CERT)
        assert base64.b64encode(_DUMMY_CERT).decode() in kc


# ── Integration: RBAC proxy server ───────────────────────────────────────────


class _MockKubectlProxyHandler(http.server.BaseHTTPRequestHandler):
    """Minimal echo server standing in for a real kubectl proxy."""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps({"path": self.path, "method": "GET"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def mock_kubectl_proxy() -> tuple[http.server.HTTPServer, int]:
    """Start a mock kubectl proxy on a random port; yield (server, port)."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _MockKubectlProxyHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield server, port
    server.shutdown()


@pytest.fixture()
def rbac_proxy(mock_kubectl_proxy: tuple[http.server.HTTPServer, int]):
    """Start the RBAC proxy (TLS) pointing at the mock kubectl proxy."""
    _, kube_port = mock_kubectl_proxy
    rules = [
        KubectlRBACRule(verbs=["get", "list", "watch"], resources=["pods"], namespaces=["*"]),
    ]
    token = "test-token-12345"
    server, port, cert_pem = start_rbac_proxy(rules, token, kube_port)
    yield server, port, token, cert_pem
    stop_rbac_proxy(server)


def _ssl_ctx_for_cert(cert_pem: bytes) -> ssl.SSLContext:
    """Return an SSLContext that trusts exactly the given self-signed cert."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    # Write cert to a temp file so load_verify_locations can read it.
    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as f:
        f.write(cert_pem)
        tmp_path = f.name
    ctx.load_verify_locations(tmp_path)
    import os

    os.unlink(tmp_path)
    return ctx


def _request(
    port: int,
    path: str,
    method: str = "GET",
    token: str | None = None,
    cert_pem: bytes | None = None,
) -> tuple[int, bytes]:
    """Send an HTTPS request to the RBAC proxy and return (status, body)."""
    ssl_ctx = _ssl_ctx_for_cert(cert_pem) if cert_pem else None
    conn = http.client.HTTPSConnection("127.0.0.1", port, context=ssl_ctx)
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    conn.request(method, path, headers=headers)
    resp = conn.getresponse()
    return resp.status, resp.read()


class TestRBACProxyIntegration:
    def test_rejects_missing_token(self, rbac_proxy: tuple) -> None:
        _, port, _, cert_pem = rbac_proxy
        status, _ = _request(port, "/api/v1/namespaces/default/pods", cert_pem=cert_pem)
        assert status == 401

    def test_rejects_wrong_token(self, rbac_proxy: tuple) -> None:
        _, port, _, cert_pem = rbac_proxy
        status, _ = _request(port, "/api/v1/namespaces/default/pods", token="wrong", cert_pem=cert_pem)
        assert status == 401

    def test_blocks_exec_subresource(self, rbac_proxy: tuple) -> None:
        _, port, token, cert_pem = rbac_proxy
        status, body = _request(port, "/api/v1/namespaces/default/pods/foo/exec", token=token, cert_pem=cert_pem)
        assert status == 403
        err = json.loads(body)
        assert "exec" in err["error"]

    def test_blocks_attach_subresource(self, rbac_proxy: tuple) -> None:
        _, port, token, cert_pem = rbac_proxy
        status, _ = _request(port, "/api/v1/namespaces/default/pods/foo/attach", token=token, cert_pem=cert_pem)
        assert status == 403

    def test_blocks_portforward_subresource(self, rbac_proxy: tuple) -> None:
        _, port, token, cert_pem = rbac_proxy
        status, _ = _request(port, "/api/v1/namespaces/default/pods/foo/portforward", token=token, cert_pem=cert_pem)
        assert status == 403

    def test_allows_permitted_get(self, rbac_proxy: tuple) -> None:
        _, port, token, cert_pem = rbac_proxy
        status, body = _request(port, "/api/v1/namespaces/default/pods", token=token, cert_pem=cert_pem)
        assert status == 200
        data = json.loads(body)
        assert data["path"] == "/api/v1/namespaces/default/pods"

    def test_denies_forbidden_verb(self, rbac_proxy: tuple) -> None:
        _, port, token, cert_pem = rbac_proxy
        # DELETE is not in the test rules
        status, body = _request(
            port, "/api/v1/namespaces/default/pods/foo", method="DELETE", token=token, cert_pem=cert_pem
        )
        assert status == 403
        err = json.loads(body)
        assert "not permitted" in err["error"]

    def test_denies_forbidden_resource(self, rbac_proxy: tuple) -> None:
        _, port, token, cert_pem = rbac_proxy
        # secrets not in rules
        status, _ = _request(port, "/api/v1/namespaces/default/secrets", token=token, cert_pem=cert_pem)
        assert status == 403

    def test_allows_discovery_endpoint(self, rbac_proxy: tuple) -> None:
        """Discovery endpoints (/api, /apis) should pass through without RBAC check."""
        _, port, token, cert_pem = rbac_proxy
        status, _ = _request(port, "/api", token=token, cert_pem=cert_pem)
        assert status == 200

    def test_allows_version_endpoint(self, rbac_proxy: tuple) -> None:
        _, port, token, cert_pem = rbac_proxy
        status, _ = _request(port, "/version", token=token, cert_pem=cert_pem)
        assert status == 200
