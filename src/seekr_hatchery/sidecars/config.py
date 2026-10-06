"""Configuration owned by built-in sandbox sidecars."""

from pydantic import BaseModel, ConfigDict

from seekr_hatchery.models import KubectlConfig
from seekr_hatchery.sidecars.oci_sidecar.config import OciConfig
from seekr_hatchery.sidecars.s3_sidecar.config import S3Config


class SidecarConfig(BaseModel):
    """Configuration fields consumed by built-in sidecars."""

    model_config = ConfigDict(extra="forbid")
    kubernetes: KubectlConfig | None = None
    oci: OciConfig | None = None
    s3: S3Config | None = None
