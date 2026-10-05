"""Configuration owned by built-in sandbox sidecars."""

from pydantic import BaseModel, ConfigDict

from seekr_hatchery.models import KubectlConfig


class SidecarConfig(BaseModel):
    """Configuration fields consumed by built-in sidecars."""

    model_config = ConfigDict(extra="forbid")
    kubernetes: KubectlConfig | None = None
