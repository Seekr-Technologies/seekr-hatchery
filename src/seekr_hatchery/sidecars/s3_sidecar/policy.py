"""Path-policy classification for proxied S3 data-plane requests."""

import http.client
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit

from seekr_hatchery.sidecars.s3_sidecar.config import S3Permission, S3Rule


@dataclass(frozen=True)
class PolicyTarget:
    """One permission check derived from an S3 HTTP request."""

    permission: S3Permission
    bucket: str
    key: str


def _bucket_and_key(path: str) -> tuple[str, str] | None:
    parts = path.lstrip("/").split("/", 1)
    if not parts[0]:
        return None
    bucket = unquote(parts[0])
    key = unquote(parts[1]) if len(parts) == 2 else ""
    return bucket, key


def _copy_source(value: str) -> tuple[str, str] | None:
    source = urlsplit(value.lstrip("/"))
    return _bucket_and_key(source.path)


def policy_targets(
    method: str,
    request_target: str,
    headers: http.client.HTTPMessage,
) -> list[PolicyTarget] | None:
    """Classify supported S3 data-plane requests; return None when ambiguous."""
    parsed = urlsplit(request_target)
    resource = _bucket_and_key(parsed.path)
    if resource is None:
        return None
    bucket, key = resource
    query = parse_qs(parsed.query, keep_blank_values=True)
    query.pop("x-id", None)
    query_keys = set(query)

    if not key:
        list_keys = {
            "list-type",
            "prefix",
            "delimiter",
            "continuation-token",
            "start-after",
            "max-keys",
            "encoding-type",
            "marker",
        }
        prefixes = query.get("prefix", [""])
        if len(prefixes) != 1:
            return None
        if method in {"GET", "HEAD"} and query_keys <= list_keys:
            return [PolicyTarget("LIST", bucket, prefixes[0])]
        if (
            method == "GET"
            and "uploads" in query
            and query_keys <= (list_keys | {"uploads", "key-marker", "upload-id-marker", "max-uploads"})
        ):
            return [PolicyTarget("WRITE", bucket, prefixes[0])]
        return None

    response_keys = {name for name in query_keys if name.startswith("response-")}
    ordinary_read_keys = {"versionId", "partNumber"} | response_keys
    if method in {"GET", "HEAD"}:
        if "uploadId" in query and query_keys <= {"uploadId", "max-parts", "part-number-marker"}:
            return [PolicyTarget("WRITE", bucket, key)]
        if query_keys <= ordinary_read_keys:
            return [PolicyTarget("READ", bucket, key)]
        return None

    if method == "PUT":
        multipart_keys = {"uploadId", "partNumber"}
        if query_keys and not (multipart_keys <= query_keys and query_keys <= multipart_keys):
            return None
        targets = [PolicyTarget("WRITE", bucket, key)]
        copy_source = headers.get("X-Amz-Copy-Source")
        if copy_source:
            source = _copy_source(copy_source)
            if source is None:
                return None
            targets.append(PolicyTarget("READ", source[0], source[1]))
        return targets

    if method == "POST":
        if query_keys == {"uploads"} or ("uploadId" in query and query_keys == {"uploadId"}):
            return [PolicyTarget("WRITE", bucket, key)]
        return None

    if method == "DELETE":
        if "uploadId" in query and query_keys == {"uploadId"}:
            return [PolicyTarget("WRITE", bucket, key)]
        if query_keys <= {"versionId"}:
            return [PolicyTarget("DELETE", bucket, key)]
        return None

    return None


def rules_allow(rules: list[S3Rule] | None, targets: list[PolicyTarget] | None) -> bool:
    """Return whether every target is covered; absent rules preserve host access."""
    if rules is None:
        return True
    if targets is None:
        return False
    return all(
        any(
            target.permission in rule.permissions
            and target.bucket == rule.bucket
            and target.key.startswith(rule.prefix)
            for rule in rules
        )
        for target in targets
    )
