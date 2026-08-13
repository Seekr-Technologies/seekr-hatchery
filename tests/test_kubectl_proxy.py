"""Tests for the kubectl RBAC proxy module."""

from __future__ import annotations

import http.client
import http.server
import json
import ssl
import tempfile
import threading

import pytest
import yaml
from pydantic import ValidationError

from seekr_hatchery.kubectl_proxy import (
    KubectlConfig,
    KubectlContext,
    KubectlRBACRule,
    _generate_self_signed_cert,
    check_rbac,
    http_method_to_k8s_verbs,
    make_kubeconfig,
    parse_k8s_url,
    start_context_proxies,
    start_rbac_proxy,
    stop_context_proxies,
    stop_rbac_proxy,
)

# ── URL parsing ───────────────────────────────────────────────────────────────


class TestParseK8sUrl:
    def test_core_namespaced_collection(self) -> None:
        assert parse_k8s_url("/api/v1/namespaces/default/pods") == ("default", "pods", "", False, False)

    def test_core_namespaced_named_resource(self) -> None:
        assert parse_k8s_url("/api/v1/namespaces/staging/pods/my-pod") == ("staging", "pods", "", True, False)

    def test_core_namespaced_subresource(self) -> None:
        assert parse_k8s_url("/api/v1/namespaces/default/pods/my-pod/exec") == (
            "default",
            "pods",
            "exec",
            True,
            False,
        )

    def test_core_namespaced_log_subresource(self) -> None:
        assert parse_k8s_url("/api/v1/namespaces/default/pods/my-pod/log") == ("default", "pods", "log", True, False)

    def test_core_cluster_scoped(self) -> None:
        assert parse_k8s_url("/api/v1/nodes") == ("", "nodes", "", False, False)

    def test_core_cluster_scoped_named(self) -> None:
        assert parse_k8s_url("/api/v1/nodes/my-node") == ("", "nodes", "", True, False)

    def test_group_namespaced_collection(self) -> None:
        assert parse_k8s_url("/apis/apps/v1/namespaces/default/deployments") == (
            "default",
            "deployments",
            "",
            False,
            False,
        )

    def test_group_namespaced_named(self) -> None:
        assert parse_k8s_url("/apis/apps/v1/namespaces/staging/deployments/my-dep") == (
            "staging",
            "deployments",
            "",
            True,
            False,
        )

    def test_group_cluster_scoped(self) -> None:
        assert parse_k8s_url("/apis/apps/v1/deployments") == ("", "deployments", "", False, False)

    def test_discovery_api(self) -> None:
        assert parse_k8s_url("/api") == ("", "", "", False, False)

    def test_discovery_apis(self) -> None:
        assert parse_k8s_url("/apis") == ("", "", "", False, False)

    def test_healthz(self) -> None:
        assert parse_k8s_url("/healthz") == ("", "", "", False, False)

    def test_version(self) -> None:
        assert parse_k8s_url("/version") == ("", "", "", False, False)

    def test_trailing_slash_stripped(self) -> None:
        assert parse_k8s_url("/api/v1/namespaces/default/pods/") == ("default", "pods", "", False, False)

    def test_portforward_subresource(self) -> None:
        assert parse_k8s_url("/api/v1/namespaces/default/pods/my-pod/portforward") == (
            "default",
            "pods",
            "portforward",
            True,
            False,
        )

    def test_watch_query_collection(self) -> None:
        assert parse_k8s_url("/api/v1/namespaces/default/pods?watch=true") == (
            "default",
            "pods",
            "",
            False,
            True,
        )

    def test_watch_query_named_resource(self) -> None:
        """Watching a single named object still resolves to has_name=True, is_watch=True."""
        assert parse_k8s_url("/api/v1/namespaces/default/pods/my-pod?watch=true") == (
            "default",
            "pods",
            "",
            True,
            True,
        )

    def test_watch_query_false_is_not_watch(self) -> None:
        assert parse_k8s_url("/api/v1/namespaces/default/pods?watch=false") == (
            "default",
            "pods",
            "",
            False,
            False,
        )

    def test_watch_query_other_params_ignored(self) -> None:
        assert parse_k8s_url("/api/v1/namespaces/default/pods?labelSelector=app%3Dfoo") == (
            "default",
            "pods",
            "",
            False,
            False,
        )

    def test_no_query_string_is_not_watch(self) -> None:
        assert parse_k8s_url("/api/v1/namespaces/default/pods") == ("default", "pods", "", False, False)


# ── HTTP verb mapping ─────────────────────────────────────────────────────────


class TestHttpMethodToK8sVerbs:
    def test_get_collection(self) -> None:
        assert http_method_to_k8s_verbs("GET", has_name=False, is_watch=False) == ["list"]

    def test_get_named(self) -> None:
        assert http_method_to_k8s_verbs("GET", has_name=True, is_watch=False) == ["get"]

    def test_get_watch_collection(self) -> None:
        assert http_method_to_k8s_verbs("GET", has_name=False, is_watch=True) == ["watch"]

    def test_get_watch_named(self) -> None:
        """Watch takes precedence over has_name — watching one object is still 'watch'."""
        assert http_method_to_k8s_verbs("GET", has_name=True, is_watch=True) == ["watch"]

    def test_post(self) -> None:
        assert http_method_to_k8s_verbs("POST", has_name=False, is_watch=False) == ["create"]

    def test_put(self) -> None:
        assert http_method_to_k8s_verbs("PUT", has_name=True, is_watch=False) == ["update"]

    def test_patch(self) -> None:
        assert http_method_to_k8s_verbs("PATCH", has_name=True, is_watch=False) == ["patch"]

    def test_delete_named(self) -> None:
        assert http_method_to_k8s_verbs("DELETE", has_name=True, is_watch=False) == ["delete"]

    def test_delete_collection(self) -> None:
        assert http_method_to_k8s_verbs("DELETE", has_name=False, is_watch=False) == ["deletecollection"]

    def test_case_insensitive(self) -> None:
        assert http_method_to_k8s_verbs("get", has_name=False, is_watch=False) == ["list"]


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

    def test_list_only_rule_permits_list_verbs(self) -> None:
        rules = [KubectlRBACRule(verbs=["list"], resources=["secrets"], namespaces=["*"])]
        assert check_rbac(rules, ["list", "watch"], "secrets", "default") is True

    def test_list_only_rule_denies_get_verb(self) -> None:
        """A list-only rule must not also grant get (the bug this suite guards against)."""
        rules = [KubectlRBACRule(verbs=["list"], resources=["secrets"], namespaces=["*"])]
        assert check_rbac(rules, ["get"], "secrets", "default") is False

    def test_get_only_rule_permits_get_verb(self) -> None:
        rules = [KubectlRBACRule(verbs=["get"], resources=["secrets"], namespaces=["*"])]
        assert check_rbac(rules, ["get"], "secrets", "default") is True

    def test_get_only_rule_denies_list_verbs(self) -> None:
        rules = [KubectlRBACRule(verbs=["get"], resources=["secrets"], namespaces=["*"])]
        assert check_rbac(rules, ["list", "watch"], "secrets", "default") is False

    def test_get_list_watch_rule_permits_both(self) -> None:
        rules = [KubectlRBACRule(verbs=["get", "list", "watch"], resources=["secrets"], namespaces=["*"])]
        assert check_rbac(rules, ["get"], "secrets", "default") is True
        assert check_rbac(rules, ["list", "watch"], "secrets", "default") is True


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

    def test_unknown_key_rejected(self) -> None:
        """A typo like 'contexts' → 'context s' must not be silently ignored."""
        with pytest.raises(ValidationError, match="rulez"):
            KubectlConfig.model_validate({"rulez": []})


# ── Multi-context configuration ───────────────────────────────────────────────


class TestResolvedContexts:
    def test_shorthand_becomes_single_context(self) -> None:
        cfg = KubectlConfig(
            context="my-dev-cluster",
            rules=[KubectlRBACRule(verbs=["get"], resources=["pods"])],
        )
        contexts = cfg.resolved_contexts()
        assert len(contexts) == 1
        assert contexts[0].context == "my-dev-cluster"
        assert contexts[0].display_name == "my-dev-cluster"
        assert contexts[0].rules[0].verbs == ["get"]

    def test_empty_config_yields_one_unpinned_context(self) -> None:
        contexts = KubectlConfig().resolved_contexts()
        assert len(contexts) == 1
        assert contexts[0].context is None
        assert contexts[0].display_name == "hatchery-proxy"
        assert contexts[0].rules == []  # fail-closed

    def test_list_form_preserves_order_and_per_context_rules(self) -> None:
        cfg = KubectlConfig.model_validate(
            {
                "contexts": [
                    {"context": "dev", "rules": [{"verbs": ["get", "delete"], "resources": ["pods"]}]},
                    {"context": "prd", "rules": [{"verbs": ["get"], "resources": ["pods"]}]},
                ]
            }
        )
        contexts = cfg.resolved_contexts()
        assert [c.display_name for c in contexts] == ["dev", "prd"]
        assert contexts[0].rules[0].verbs == ["get", "delete"]
        assert contexts[1].rules[0].verbs == ["get"]

    @pytest.mark.parametrize(
        "extra",
        [{"context": "other"}, {"rules": [{"verbs": ["get"], "resources": ["pods"]}]}],
    )
    def test_contexts_cannot_be_mixed_with_shorthand(self, extra: dict) -> None:
        with pytest.raises(ValidationError, match="cannot be combined"):
            KubectlConfig.model_validate({"contexts": [{"context": "dev"}], **extra})

    def test_duplicate_context_names_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate context name"):
            KubectlConfig.model_validate({"contexts": [{"context": "dev"}, {"context": "dev"}]})

    def test_unknown_key_in_context_entry_rejected(self) -> None:
        with pytest.raises(ValidationError, match="rulez"):
            KubectlConfig.model_validate({"contexts": [{"context": "dev", "rulez": []}]})


# ── make_kubeconfig ───────────────────────────────────────────────────────────


_DUMMY_CERT = b"-----BEGIN CERTIFICATE-----\nZmFrZWNlcnQ=\n-----END CERTIFICATE-----\n"


class TestMakeKubeconfig:
    def test_contains_rbac_port(self) -> None:
        kc = make_kubeconfig([("hatchery-proxy", 12345)], "my-token", _DUMMY_CERT)
        assert "12345" in kc

    def test_contains_token(self) -> None:
        kc = make_kubeconfig([("hatchery-proxy", 12345)], "my-secret-token", _DUMMY_CERT)
        assert "my-secret-token" in kc

    def test_valid_yaml(self) -> None:
        kc = make_kubeconfig([("hatchery-proxy", 8080)], "tok", _DUMMY_CERT)
        parsed = yaml.safe_load(kc)
        assert parsed["kind"] == "Config"
        assert parsed["current-context"] == "hatchery-proxy"

    def test_uses_https(self) -> None:
        kc = make_kubeconfig([("hatchery-proxy", 8080)], "tok", _DUMMY_CERT)
        assert "https://" in kc

    def test_embeds_ca_cert(self) -> None:
        import base64

        kc = make_kubeconfig([("hatchery-proxy", 8080)], "tok", _DUMMY_CERT)
        assert base64.b64encode(_DUMMY_CERT).decode() in kc

    def test_requires_at_least_one_context(self) -> None:
        with pytest.raises(ValueError, match="at least one context"):
            make_kubeconfig([], "tok", _DUMMY_CERT)

    def test_single_named_context_keeps_hatchery_proxy_alias(self) -> None:
        """Anything relying on the historical fixed context name keeps working."""
        parsed = yaml.safe_load(make_kubeconfig([("dev", 8080)], "tok", _DUMMY_CERT))
        by_name = {c["name"]: c for c in parsed["contexts"]}
        assert set(by_name) == {"dev", "hatchery-proxy"}
        assert by_name["hatchery-proxy"]["context"]["cluster"] == "dev"
        assert parsed["current-context"] == "dev"
        assert len(parsed["clusters"]) == 1

    def test_multiple_contexts(self) -> None:
        parsed = yaml.safe_load(make_kubeconfig([("dev", 1111), ("prd", 2222)], "tok", _DUMMY_CERT))

        # First entry is the default; no alias once the agent has a real choice.
        assert parsed["current-context"] == "dev"
        assert [c["name"] for c in parsed["contexts"]] == ["dev", "prd"]

        clusters = {c["name"]: c["cluster"] for c in parsed["clusters"]}
        assert clusters["dev"]["server"] == "https://host.docker.internal:1111"
        assert clusters["prd"]["server"] == "https://host.docker.internal:2222"

        # One shared CA and one shared user across contexts.
        assert clusters["dev"]["certificate-authority-data"] == clusters["prd"]["certificate-authority-data"]
        assert len(parsed["users"]) == 1
        assert {c["context"]["user"] for c in parsed["contexts"]} == {parsed["users"][0]["name"]}

    def test_context_name_needing_yaml_quoting(self) -> None:
        """Real EKS context names contain ':' and '/' — they must survive a round trip."""
        eks = "arn:aws:eks:us-west-2:123456789012:cluster/prd"
        parsed = yaml.safe_load(make_kubeconfig([(eks, 1111)], "tok", _DUMMY_CERT))
        assert parsed["current-context"] == eks
        assert parsed["clusters"][0]["name"] == eks


# ── Integration: RBAC proxy server ───────────────────────────────────────────


class _MockKubectlProxyHandler(http.server.BaseHTTPRequestHandler):
    """Minimal echo server standing in for a real kubectl proxy."""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def do_GET(self) -> None:  # noqa: N802
        self._echo("GET")

    def do_DELETE(self) -> None:  # noqa: N802
        self._echo("DELETE")

    def _echo(self, method: str) -> None:
        body = json.dumps({"path": self.path, "method": method}).encode()
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


@pytest.fixture()
def list_only_rbac_proxy(mock_kubectl_proxy: tuple[http.server.HTTPServer, int]):
    """RBAC proxy with a list-only rule on secrets — get must be denied."""
    _, kube_port = mock_kubectl_proxy
    rules = [
        KubectlRBACRule(verbs=["list"], resources=["secrets"], namespaces=["*"]),
    ]
    token = "test-token-list-only"
    server, port, cert_pem = start_rbac_proxy(rules, token, kube_port)
    yield server, port, token, cert_pem
    stop_rbac_proxy(server)


@pytest.fixture()
def get_only_rbac_proxy(mock_kubectl_proxy: tuple[http.server.HTTPServer, int]):
    """RBAC proxy with a get-only rule on secrets — list must be denied."""
    _, kube_port = mock_kubectl_proxy
    rules = [
        KubectlRBACRule(verbs=["get"], resources=["secrets"], namespaces=["*"]),
    ]
    token = "test-token-get-only"
    server, port, cert_pem = start_rbac_proxy(rules, token, kube_port)
    yield server, port, token, cert_pem
    stop_rbac_proxy(server)


class TestRBACProxyGetListDistinction:
    """Regression coverage: a list-only rule must not also grant get, and vice versa."""

    def test_list_only_allows_collection(self, list_only_rbac_proxy: tuple) -> None:
        _, port, token, cert_pem = list_only_rbac_proxy
        status, _ = _request(port, "/api/v1/namespaces/default/secrets", token=token, cert_pem=cert_pem)
        assert status == 200

    def test_list_only_denies_named_object(self, list_only_rbac_proxy: tuple) -> None:
        _, port, token, cert_pem = list_only_rbac_proxy
        status, _ = _request(port, "/api/v1/namespaces/default/secrets/my-secret", token=token, cert_pem=cert_pem)
        assert status == 403

    def test_get_only_denies_collection(self, get_only_rbac_proxy: tuple) -> None:
        _, port, token, cert_pem = get_only_rbac_proxy
        status, _ = _request(port, "/api/v1/namespaces/default/secrets", token=token, cert_pem=cert_pem)
        assert status == 403

    def test_get_only_allows_named_object(self, get_only_rbac_proxy: tuple) -> None:
        _, port, token, cert_pem = get_only_rbac_proxy
        status, _ = _request(port, "/api/v1/namespaces/default/secrets/my-secret", token=token, cert_pem=cert_pem)
        assert status == 200


@pytest.fixture()
def watch_only_rbac_proxy(mock_kubectl_proxy: tuple[http.server.HTTPServer, int]):
    """RBAC proxy with a watch-only rule on secrets — get/list must be denied."""
    _, kube_port = mock_kubectl_proxy
    rules = [
        KubectlRBACRule(verbs=["watch"], resources=["secrets"], namespaces=["*"]),
    ]
    token = "test-token-watch-only"
    server, port, cert_pem = start_rbac_proxy(rules, token, kube_port)
    yield server, port, token, cert_pem
    stop_rbac_proxy(server)


class TestRBACProxyWatchDistinction:
    """Regression coverage: watch is a distinct verb from get/list, not implied by either."""

    def test_watch_only_allows_watch_on_collection(self, watch_only_rbac_proxy: tuple) -> None:
        _, port, token, cert_pem = watch_only_rbac_proxy
        status, _ = _request(port, "/api/v1/namespaces/default/secrets?watch=true", token=token, cert_pem=cert_pem)
        assert status == 200

    def test_watch_only_denies_plain_list(self, watch_only_rbac_proxy: tuple) -> None:
        _, port, token, cert_pem = watch_only_rbac_proxy
        status, _ = _request(port, "/api/v1/namespaces/default/secrets", token=token, cert_pem=cert_pem)
        assert status == 403

    def test_watch_only_denies_plain_get(self, watch_only_rbac_proxy: tuple) -> None:
        _, port, token, cert_pem = watch_only_rbac_proxy
        status, _ = _request(port, "/api/v1/namespaces/default/secrets/my-secret", token=token, cert_pem=cert_pem)
        assert status == 403

    def test_list_only_denies_watch(self, list_only_rbac_proxy: tuple) -> None:
        _, port, token, cert_pem = list_only_rbac_proxy
        status, _ = _request(port, "/api/v1/namespaces/default/secrets?watch=true", token=token, cert_pem=cert_pem)
        assert status == 403


@pytest.fixture()
def delete_only_rbac_proxy(mock_kubectl_proxy: tuple[http.server.HTTPServer, int]):
    """RBAC proxy with a delete-only rule on secrets — deletecollection must be denied."""
    _, kube_port = mock_kubectl_proxy
    rules = [
        KubectlRBACRule(verbs=["delete"], resources=["secrets"], namespaces=["*"]),
    ]
    token = "test-token-delete-only"
    server, port, cert_pem = start_rbac_proxy(rules, token, kube_port)
    yield server, port, token, cert_pem
    stop_rbac_proxy(server)


@pytest.fixture()
def deletecollection_only_rbac_proxy(mock_kubectl_proxy: tuple[http.server.HTTPServer, int]):
    """RBAC proxy with a deletecollection-only rule on secrets — delete must be denied."""
    _, kube_port = mock_kubectl_proxy
    rules = [
        KubectlRBACRule(verbs=["deletecollection"], resources=["secrets"], namespaces=["*"]),
    ]
    token = "test-token-deletecollection-only"
    server, port, cert_pem = start_rbac_proxy(rules, token, kube_port)
    yield server, port, token, cert_pem
    stop_rbac_proxy(server)


class TestRBACProxyDeleteDistinction:
    """Regression coverage: delete and deletecollection must be independently grantable."""

    def test_delete_only_allows_named_delete(self, delete_only_rbac_proxy: tuple) -> None:
        _, port, token, cert_pem = delete_only_rbac_proxy
        status, _ = _request(
            port, "/api/v1/namespaces/default/secrets/my-secret", method="DELETE", token=token, cert_pem=cert_pem
        )
        assert status == 200

    def test_delete_only_denies_deletecollection(self, delete_only_rbac_proxy: tuple) -> None:
        _, port, token, cert_pem = delete_only_rbac_proxy
        status, _ = _request(
            port, "/api/v1/namespaces/default/secrets", method="DELETE", token=token, cert_pem=cert_pem
        )
        assert status == 403

    def test_deletecollection_only_allows_collection_delete(self, deletecollection_only_rbac_proxy: tuple) -> None:
        _, port, token, cert_pem = deletecollection_only_rbac_proxy
        status, _ = _request(
            port, "/api/v1/namespaces/default/secrets", method="DELETE", token=token, cert_pem=cert_pem
        )
        assert status == 200

    def test_deletecollection_only_denies_named_delete(self, deletecollection_only_rbac_proxy: tuple) -> None:
        _, port, token, cert_pem = deletecollection_only_rbac_proxy
        status, _ = _request(
            port, "/api/v1/namespaces/default/secrets/my-secret", method="DELETE", token=token, cert_pem=cert_pem
        )
        assert status == 403


class TestCertGeneration:
    def test_validity_outlives_long_sessions(self) -> None:
        """Cert validity must outlive any realistic session.

        See ``_generate_self_signed_cert`` for why rotation is unnecessary
        in this system; this test guards against a quiet revert to a short
        validity that would silently break long-running sandboxes.
        """
        import datetime

        from cryptography import x509

        cert_pem, _ = _generate_self_signed_cert()
        cert = x509.load_pem_x509_certificate(cert_pem)
        delta = cert.not_valid_after_utc - cert.not_valid_before_utc
        assert delta >= datetime.timedelta(days=365)


# ── Multi-context orchestration ───────────────────────────────────────────────


class _FakeProc:
    """Stand-in for the ``kubectl proxy`` Popen object."""

    def __init__(self, context: str | None) -> None:
        self.context = context
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


@pytest.fixture()
def fake_kubectl_proxies(
    monkeypatch: pytest.MonkeyPatch,
    mock_kubectl_proxy: tuple[http.server.HTTPServer, int],
) -> list[_FakeProc]:
    """Patch out the real ``kubectl proxy`` subprocess.

    Every context resolves to the shared mock kubectl proxy, except contexts
    named ``broken-*``, which raise as an unreachable cluster would.  Returns the
    list of fake procs that were handed out.
    """
    _, kube_port = mock_kubectl_proxy
    procs: list[_FakeProc] = []

    def fake_start(context: str | None = None, timeout: float = 10.0) -> tuple[_FakeProc, int]:
        if context and context.startswith("broken"):
            raise RuntimeError(
                f"kubectl proxy exited unexpectedly.  stderr: no context exists with the name: {context}"
            )
        proc = _FakeProc(context)
        procs.append(proc)
        return proc, kube_port

    monkeypatch.setattr("seekr_hatchery.kubectl_proxy._require_kubectl", lambda: None)
    monkeypatch.setattr("seekr_hatchery.kubectl_proxy.start_kubectl_proxy_proc", fake_start)
    return procs


class TestStartContextProxies:
    def test_starts_one_proxy_per_context_in_order(self, fake_kubectl_proxies: list[_FakeProc]) -> None:
        contexts = [KubectlContext(context="dev"), KubectlContext(context="prd")]
        started, failures, ca_cert_pem = start_context_proxies(contexts, "tok")
        try:
            assert failures == []
            assert [p.name for p in started] == ["dev", "prd"]
            assert [p.kubectl_proc.context for p in started] == ["dev", "prd"]  # type: ignore[attr-defined]
            # Distinct listeners, but one cert so the kubeconfig needs one CA.
            assert len({p.rbac_port for p in started}) == 2
            assert ca_cert_pem.startswith(b"-----BEGIN CERTIFICATE-----")
        finally:
            stop_context_proxies(started)

    def test_failed_context_is_reported_not_raised(self, fake_kubectl_proxies: list[_FakeProc]) -> None:
        contexts = [
            KubectlContext(context="dev"),
            KubectlContext(context="broken-prd"),
            KubectlContext(context="stg"),
        ]
        started, failures, _ = start_context_proxies(contexts, "tok")
        try:
            assert [p.name for p in started] == ["dev", "stg"]
            assert len(failures) == 1
            name, err = failures[0]
            assert name == "broken-prd"
            assert "no context exists" in err
        finally:
            stop_context_proxies(started)

    def test_all_contexts_failing_yields_empty_started(self, fake_kubectl_proxies: list[_FakeProc]) -> None:
        started, failures, _ = start_context_proxies([KubectlContext(context="broken-dev")], "tok")
        assert started == []
        assert len(failures) == 1

    def test_missing_kubectl_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda _: None)
        with pytest.raises(RuntimeError, match="kubectl not found"):
            start_context_proxies([KubectlContext(context="dev")], "tok")

    def test_stop_terminates_every_proxy(self, fake_kubectl_proxies: list[_FakeProc]) -> None:
        started, _, _ = start_context_proxies([KubectlContext(context="dev"), KubectlContext(context="prd")], "tok")
        stop_context_proxies(started)
        assert all(p.kubectl_proc.terminated for p in started)  # type: ignore[attr-defined]
        # Sockets are closed, so nothing is listening on those ports any more.
        for p in started:
            with pytest.raises(OSError):
                _request(p.rbac_port, "/api/v1/namespaces/default/pods", token="tok")

    def test_each_context_enforces_its_own_rules(self, fake_kubectl_proxies: list[_FakeProc]) -> None:
        """The point of per-context rules: read-write dev, read-only prd."""
        contexts = [
            KubectlContext(
                context="dev",
                rules=[KubectlRBACRule(verbs=["get", "delete"], resources=["pods"], namespaces=["*"])],
            ),
            KubectlContext(
                context="prd",
                rules=[KubectlRBACRule(verbs=["get"], resources=["pods"], namespaces=["*"])],
            ),
        ]
        started, failures, ca_cert_pem = start_context_proxies(contexts, "tok")
        try:
            assert failures == []
            dev, prd = started
            path = "/api/v1/namespaces/default/pods/my-pod"

            # Both may read.
            for p in (dev, prd):
                status, _ = _request(p.rbac_port, path, token="tok", cert_pem=ca_cert_pem)
                assert status == 200

            # Only dev may delete.
            status, _ = _request(dev.rbac_port, path, method="DELETE", token="tok", cert_pem=ca_cert_pem)
            assert status == 200
            status, _ = _request(prd.rbac_port, path, method="DELETE", token="tok", cert_pem=ca_cert_pem)
            assert status == 403
        finally:
            stop_context_proxies(started)

    def test_shared_cert_is_trusted_by_every_context(self, fake_kubectl_proxies: list[_FakeProc]) -> None:
        contexts = [
            KubectlContext(context=name, rules=[KubectlRBACRule(verbs=["get"], resources=["pods"])])
            for name in ("dev", "prd")
        ]
        started, _, ca_cert_pem = start_context_proxies(contexts, "tok")
        try:
            for p in started:
                # Verification is on (cert_pem given) — a per-proxy cert would fail here.
                status, _ = _request(
                    p.rbac_port, "/api/v1/namespaces/default/pods/x", token="tok", cert_pem=ca_cert_pem
                )
                assert status == 200
        finally:
            stop_context_proxies(started)
