"""S3 path-policy contracts."""

from email.parser import BytesParser

import pytest

from seekr_hatchery.sidecars.s3_sidecar import S3Config, policy


class TestS3Policy:
    @staticmethod
    def _headers(**headers: str):
        payload = "".join(f"{name}: {value}\n" for name, value in headers.items()).encode() + b"\n"
        return BytesParser().parsebytes(payload)

    @pytest.mark.parametrize(
        ("method", "target", "permission", "bucket", "key"),
        [
            ("GET", "/artifacts/team-a/file.txt", "READ", "artifacts", "team-a/file.txt"),
            ("GET", "/artifacts/team-a/file.txt?x-id=GetObject", "READ", "artifacts", "team-a/file.txt"),
            ("PUT", "/artifacts/team-a/file.txt", "WRITE", "artifacts", "team-a/file.txt"),
            ("PUT", "/artifacts/team-a/file.txt?x-id=PutObject", "WRITE", "artifacts", "team-a/file.txt"),
            ("DELETE", "/artifacts/team-a/file.txt?versionId=1", "DELETE", "artifacts", "team-a/file.txt"),
            ("GET", "/artifacts?list-type=2&prefix=team-a%2F", "LIST", "artifacts", "team-a/"),
            ("POST", "/artifacts/team-a/file?uploads", "WRITE", "artifacts", "team-a/file"),
            ("PUT", "/artifacts/team-a/file?uploadId=u&partNumber=1", "WRITE", "artifacts", "team-a/file"),
            ("GET", "/artifacts/team-a/file?uploadId=u", "WRITE", "artifacts", "team-a/file"),
            ("DELETE", "/artifacts/team-a/file?uploadId=u", "WRITE", "artifacts", "team-a/file"),
        ],
    )
    def test_classifies_supported_data_operations(
        self,
        method: str,
        target: str,
        permission: str,
        bucket: str,
        key: str,
    ) -> None:
        assert policy.policy_targets(method, target, self._headers()) == [policy.PolicyTarget(permission, bucket, key)]

    @pytest.mark.parametrize(
        ("method", "target"),
        [
            ("GET", "/"),
            ("GET", "/artifacts?acl"),
            ("PUT", "/artifacts/team-a/file?tagging"),
            ("POST", "/artifacts?delete"),
            ("DELETE", "/artifacts"),
            ("GET", "/artifacts?list-type=2&prefix=team-a%2F&prefix=team-b%2F"),
        ],
    )
    def test_rejects_administrative_bulk_and_ambiguous_operations(self, method: str, target: str) -> None:
        assert policy.policy_targets(method, target, self._headers()) is None

    def test_rules_require_bucket_prefix_and_permission(self) -> None:
        rules = (
            S3Config(
                profiles={
                    "development": {
                        "rules": [
                            {
                                "path": "s3://artifacts/team-a/",
                                "permissions": ["READ", "LIST", "WRITE"],
                            }
                        ],
                    }
                }
            )
            .profiles["development"]
            .rules
        )
        assert policy.rules_allow(rules, [policy.PolicyTarget("READ", "artifacts", "team-a/file")]) is True
        assert policy.rules_allow(rules, [policy.PolicyTarget("READ", "artifacts", "team-b/file")]) is False
        assert policy.rules_allow(rules, [policy.PolicyTarget("DELETE", "artifacts", "team-a/file")]) is False
        assert policy.rules_allow([], [policy.PolicyTarget("READ", "artifacts", "team-a/file")]) is False
        assert policy.rules_allow(None, None) is True

    def test_bucket_path_explicitly_allows_the_whole_bucket(self) -> None:
        config = S3Config(
            profiles={
                "company-dev": {
                    "rules": [{"path": "s3://artifacts", "permissions": ["READ"]}],
                }
            }
        )
        assert config.profiles["company-dev"].rules is not None
        assert config.profiles["company-dev"].rules[0].bucket == "artifacts"
        assert config.profiles["company-dev"].rules[0].prefix == ""

    def test_copy_requires_destination_write_and_source_read(self) -> None:
        targets = policy.policy_targets(
            "PUT",
            "/dest/out/copied.txt",
            self._headers(**{"X-Amz-Copy-Source": "/source/in/original.txt?versionId=1"}),
        )
        assert targets == [
            policy.PolicyTarget("WRITE", "dest", "out/copied.txt"),
            policy.PolicyTarget("READ", "source", "in/original.txt"),
        ]
        rules = (
            S3Config(
                profiles={
                    "copy": {
                        "rules": [
                            {"path": "s3://dest/out/", "permissions": ["WRITE"]},
                            {"path": "s3://source/in/", "permissions": ["READ"]},
                        ],
                    }
                }
            )
            .profiles["copy"]
            .rules
        )
        assert policy.rules_allow(rules, targets) is True
        assert policy.rules_allow(rules[:1], targets) is False
