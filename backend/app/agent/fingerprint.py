from __future__ import annotations

import json
from collections.abc import Sequence
from hashlib import sha256
from typing import TYPE_CHECKING

from .dataset_profile import DatasetProfile, MetadataProfile, MatrixProfile

if TYPE_CHECKING:
    from .validation import DatasetRef


def compute_input_fingerprint(
    *,
    owner_id: str,
    dataset_refs: Sequence[DatasetRef],
    profiles: Sequence[DatasetProfile] = (),
) -> str:
    """Hash dataset identity/checksums and contrast-relevant profile facts."""

    refs = [{
        "dataset_id": ref.dataset_id,
        "role": ref.role,
        "filename": ref.filename,
        "checksum": ref.checksum,
        "owner_id": ref.owner_id,
    } for ref in dataset_refs]
    profile_payload = []
    for profile in profiles:
        if isinstance(profile, MatrixProfile):
            profile_payload.append({
                "role": profile.role,
                "shape": profile.shape,
                "numeric_type": profile.numeric_type,
                "has_negative": profile.has_negative,
                "missing_rate": profile.missing_rate,
            })
        elif isinstance(profile, MetadataProfile):
            profile_payload.append({
                "role": profile.role,
                "columns": profile.columns,
                "levels": profile.levels,
                "alignment": profile.alignment,
            })
    payload = {
        "owner_id": owner_id,
        "datasets": sorted(
            refs,
            key=lambda item: (
                item["role"], item["dataset_id"], item["filename"],
                item["checksum"], item["owner_id"],
            ),
        ),
        "profiles": sorted(profile_payload, key=lambda item: item["role"]),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + sha256(canonical.encode("utf-8")).hexdigest()
