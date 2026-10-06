"""Path-policy classification for proxied OCI Object Storage requests."""

from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit

from seekr_hatchery.sidecars.oci_sidecar.config import OciPermission, OciRule


@dataclass(frozen=True)
class PolicyTarget:
    """One permission check derived from an Object Storage request."""

    permission: OciPermission
    namespace: str
    bucket: str
    object_name: str


def policy_targets(method: str, request_target: str) -> list[PolicyTarget] | None:
    """Classify supported OCI Object Storage data-plane requests."""
    parsed = urlsplit(request_target)
    parts = parsed.path.lstrip("/").split("/")
    if len(parts) < 4 or parts[0] != "n" or parts[2] != "b":
        return None
    namespace = unquote(parts[1])
    bucket = unquote(parts[3])
    if not namespace or not bucket:
        return None

    tail = parts[4:]
    query = parse_qs(parsed.query, keep_blank_values=True)
    if not tail:
        if method in {"GET", "HEAD"}:
            return [PolicyTarget("LIST", namespace, bucket, "")]
        return None

    collection = tail[0]
    object_name = unquote("/".join(tail[1:])) if len(tail) > 1 else ""
    if collection == "o":
        if not object_name:
            if method != "GET":
                return None
            prefixes = query.get("prefix", [""])
            if len(prefixes) != 1:
                return None
            allowed_query = {
                "prefix",
                "start",
                "end",
                "limit",
                "delimiter",
                "fields",
                "startAfter",
            }
            if not set(query) <= allowed_query:
                return None
            return [PolicyTarget("LIST", namespace, bucket, prefixes[0])]
        if method in {"GET", "HEAD"}:
            return [PolicyTarget("READ", namespace, bucket, object_name)]
        if method == "PUT":
            return [PolicyTarget("WRITE", namespace, bucket, object_name)]
        if method == "DELETE":
            return [PolicyTarget("DELETE", namespace, bucket, object_name)]
        return None

    if collection == "u":
        if not object_name:
            if method == "GET":
                prefixes = query.get("prefix", [""])
                if len(prefixes) == 1:
                    return [PolicyTarget("LIST", namespace, bucket, prefixes[0])]
            return None
        if method in {"GET", "PUT", "POST", "DELETE"}:
            return [PolicyTarget("WRITE", namespace, bucket, object_name)]
        return None

    return None


def rules_allow(rules: list[OciRule] | None, targets: list[PolicyTarget] | None) -> bool:
    """Return whether every target is covered; omitted rules preserve host access."""
    if rules is None:
        return True
    if targets is None:
        return False
    return all(
        any(
            target.permission in rule.permissions
            and target.namespace == rule.namespace
            and target.bucket == rule.bucket
            and target.object_name.startswith(rule.prefix)
            for rule in rules
        )
        for target in targets
    )
