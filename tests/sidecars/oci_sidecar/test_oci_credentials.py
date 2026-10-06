"""OCI host profile and API-key signing contracts."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from seekr_hatchery.sidecars.oci_sidecar import OciConfig, credentials


def _resolved_profile(tmp_path: Path) -> credentials.ResolvedOciProfile:
    key_file = tmp_path / "oci-api.pem"
    key_file.write_text("private key")
    return credentials.ResolvedOciProfile(
        profile_name="DEFAULT",
        key_id="ocid1.tenancy/test-user/aa:bb",
        key_file=key_file,
        pass_phrase=None,
        region="us-ashburn-1",
        endpoint_url="https://objectstorage.us-ashburn-1.oraclecloud.com",
    )


class TestHostProfiles:
    def _write_config(self, tmp_path: Path) -> tuple[Path, Path]:
        key_file = tmp_path / "oci-api.pem"
        key_file.write_text("private key")
        config_file = tmp_path / "config"
        config_file.write_text(
            "[DEFAULT]\n"
            "tenancy=ocid1.tenancy.oc1..example\n"
            "user=ocid1.user.oc1..example\n"
            "fingerprint=00:11\n"
            f"key_file={key_file}\n"
            "region=us-ashburn-1\n"
            "\n[PRODUCTION]\n"
            "region=us-phoenix-1\n"
        )
        return config_file, key_file

    def test_validation_accepts_existing_api_key_profile(self, tmp_path: Path, monkeypatch) -> None:
        config_file, _ = self._write_config(tmp_path)
        monkeypatch.setattr(credentials.shutil, "which", lambda name: "/usr/bin/openssl")
        credentials.validate_host_profiles_exist(OciConfig(config_file=str(config_file), profiles={"DEFAULT": {}}))

    def test_missing_profile_lists_existing_profiles(self, tmp_path: Path, monkeypatch) -> None:
        config_file, _ = self._write_config(tmp_path)
        monkeypatch.setattr(credentials.shutil, "which", lambda name: "/usr/bin/openssl")
        with pytest.raises(RuntimeError, match="existing profiles: DEFAULT, PRODUCTION"):
            credentials.validate_host_profiles_exist(OciConfig(config_file=str(config_file), profiles={"MISSING": {}}))

    def test_missing_key_file_fails_before_build(self, tmp_path: Path, monkeypatch) -> None:
        config_file, key_file = self._write_config(tmp_path)
        key_file.unlink()
        monkeypatch.setattr(credentials.shutil, "which", lambda name: "/usr/bin/openssl")
        with pytest.raises(RuntimeError, match="key file"):
            credentials.validate_host_profiles_exist(OciConfig(config_file=str(config_file), profiles={"DEFAULT": {}}))

    def test_resolves_standard_profile_and_endpoint(self, tmp_path: Path) -> None:
        config_file, key_file = self._write_config(tmp_path)
        config = OciConfig(config_file=str(config_file), profiles={"DEFAULT": {}})
        resolved = credentials.resolve_oci_profile(config, "DEFAULT", config.profiles["DEFAULT"])
        assert resolved == credentials.ResolvedOciProfile(
            profile_name="DEFAULT",
            key_id="ocid1.tenancy.oc1..example/ocid1.user.oc1..example/00:11",
            key_file=key_file,
            pass_phrase=None,
            region="us-ashburn-1",
            endpoint_url="https://objectstorage.us-ashburn-1.oraclecloud.com",
        )


class TestOciSigning:
    def test_signs_expected_headers_with_host_key(self, tmp_path: Path, monkeypatch) -> None:
        captured: dict[str, object] = {}

        def fake_run(command, **kwargs):
            captured.update({"command": command, **kwargs})
            return SimpleNamespace(returncode=0, stdout=b"signature")

        monkeypatch.setattr(credentials.subprocess, "run", fake_run)
        profile = _resolved_profile(tmp_path)
        headers = {
            "host": "objectstorage.us-ashburn-1.oraclecloud.com",
            "date": "Mon, 05 Oct 2026 12:00:00 GMT",
            "content-length": "3",
            "content-type": "application/octet-stream",
            "x-content-sha256": "hash",
        }
        signed = credentials.sign_request(
            "PUT",
            "/n/ns/b/bucket/o/object",
            headers,
            profile,
        )
        assert captured["command"] == [
            "openssl",
            "dgst",
            "-sha256",
            "-sign",
            str(profile.key_file),
        ]
        assert captured["input"] == (
            b"date: Mon, 05 Oct 2026 12:00:00 GMT\n"
            b"(request-target): put /n/ns/b/bucket/o/object\n"
            b"host: objectstorage.us-ashburn-1.oraclecloud.com\n"
            b"content-length: 3\n"
            b"content-type: application/octet-stream\n"
            b"x-content-sha256: hash"
        )
        assert signed["Authorization"].startswith('Signature algorithm="rsa-sha256",')

    def test_streaming_upload_without_hash_signs_only_generic_headers(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        captured: dict[str, object] = {}

        def fake_run(command, **kwargs):
            captured.update({"command": command, **kwargs})
            return SimpleNamespace(returncode=0, stdout=b"signature")

        monkeypatch.setattr(credentials.subprocess, "run", fake_run)
        profile = _resolved_profile(tmp_path)
        headers = {
            "host": "objectstorage.us-ashburn-1.oraclecloud.com",
            "date": "Mon, 05 Oct 2026 12:00:00 GMT",
            "content-length": "66",
            "content-type": "application/octet-stream",
        }

        signed = credentials.sign_request(
            "PUT",
            "/n/ns/b/bucket/o/object",
            headers,
            profile,
        )

        assert captured["input"] == (
            b"date: Mon, 05 Oct 2026 12:00:00 GMT\n"
            b"(request-target): put /n/ns/b/bucket/o/object\n"
            b"host: objectstorage.us-ashburn-1.oraclecloud.com"
        )
        assert 'headers="date (request-target) host"' in signed["Authorization"]
