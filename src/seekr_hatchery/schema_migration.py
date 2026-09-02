"""Shared schema-version stamping for hatchery's YAML config files.

Both :mod:`seekr_hatchery.user_config` and :mod:`seekr_hatchery.repo_config`
version their on-disk schema independently, but both currently only need
the same "unversioned/absent → 1" stamp.
"""


def stamp_v1(data: dict) -> dict:
    """Bring a raw config dict from the unversioned schema ("0") up to "1" in place."""
    v = str(data.get("schema_version", "0"))

    # "0" → "1": initial versioned schema (just stamp the version)
    if v == "0":
        v = "1"

    data["schema_version"] = v
    return data
