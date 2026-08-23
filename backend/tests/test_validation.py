from __future__ import annotations

from hashlib import sha256

from backend.app.agent.dataset_profile import MetadataProfile, build_dataset_profiles
from backend.app.agent.fingerprint import compute_input_fingerprint
from backend.app.agent.param_resolver import ContrastSpec, DEGParams, MissingParam, ResolvedRequest
from backend.app.agent.validation import DatasetRef, validate_analysis_request


COUNTS = b"gene,s1,s2,s3,s4\ng1,10,12,30,32\ng2,8,9,20,22\n"
METADATA = b"sample_id,treatment\ns1,control\ns2,control\ns3,salt\ns4,salt\n"


def _refs(
    counts: bytes = COUNTS,
    metadata: bytes = METADATA,
    *,
    owner_id: str = "user-1",
) -> list[DatasetRef]:
    inputs = {
        "counts": ("counts.csv", counts),
        "metadata": ("metadata.csv", metadata),
    }
    profiles = {profile.role: profile for profile in build_dataset_profiles(inputs)}
    return [
        DatasetRef(
            dataset_id=f"dataset-{role}",
            owner_id=owner_id,
            role=role,
            filename=filename,
            checksum="sha256:" + sha256(content).hexdigest(),
            content=content,
            profile=profiles[role],
        )
        for role, (filename, content) in inputs.items()
    ]


def _request(*, min_replicates: int = 2) -> ResolvedRequest:
    return ResolvedRequest(
        analysis_type="DEG",
        params=DEGParams(
            contrast=ContrastSpec(
                compare_field="treatment", tested_level="salt", reference_level="control"
            ),
            min_replicates=min_replicates,
        ),
    )


def test_valid_request_returns_typed_preview_and_fingerprint() -> None:
    report = validate_analysis_request(_request(), _refs())

    assert report.ok
    assert report.blocking == []
    assert report.warnings == []
    assert report.preview is not None
    assert report.preview.tested_count == 2
    assert report.preview.reference_count == 2
    assert report.input_fingerprint.startswith("sha256:")


def test_sample_alignment_failure_is_preserved_as_existing_preflight_warning() -> None:
    metadata = b"sample_id,treatment\ns1,control\ns2,control\ns3,salt\ns5,salt\n"
    report = validate_analysis_request(_request(), _refs(metadata=metadata))

    assert report.ok
    assert any(issue.code == "sample_mismatch" for issue in report.warnings)


def test_negative_counts_are_blocking() -> None:
    counts = b"gene,s1,s2,s3,s4\ng1,10,-1,30,32\n"
    report = validate_analysis_request(_request(), _refs(counts=counts))

    assert not report.ok
    assert any(issue.code == "non_numeric_value" and issue.field == "counts" for issue in report.blocking)


def test_replicate_insufficient_is_blocking() -> None:
    report = validate_analysis_request(_request(min_replicates=3), _refs())

    assert not report.ok
    assert report.preview is None
    assert any(issue.field == "contrast" for issue in report.blocking)


def test_missing_resolver_params_do_not_run_as_valid_request() -> None:
    request = ResolvedRequest(
        analysis_type="DEG",
        params=None,
        missing=[MissingParam(field="tested_level", options=["salt"], reason="请选择实验组")],
    )
    report = validate_analysis_request(request, _refs())

    assert not report.ok
    assert report.missing == request.missing
    assert report.blocking[0].code == "missing_parameter"


def test_warning_only_request_remains_submittable() -> None:
    metadata = b"sample_id,treatment,unused\ns2,control,\ns1,control,\ns4,salt,\ns3,salt,\n"
    report = validate_analysis_request(_request(), _refs(metadata=metadata))

    assert report.ok
    assert report.blocking == []
    assert {issue.code for issue in report.warnings} >= {"sample_order_mismatch", "empty_column"}


def test_dataset_ownership_mismatch_blocks_validation() -> None:
    refs = _refs()
    refs[1] = refs[1].model_copy(update={"owner_id": "user-2"})

    report = validate_analysis_request(_request(), refs)

    assert not report.ok
    assert any(issue.code == "ownership_mismatch" for issue in report.blocking)


def test_declared_checksum_must_match_dataset_content() -> None:
    refs = _refs()
    refs[0] = refs[0].model_copy(update={"checksum": "sha256:" + "0" * 64})

    report = validate_analysis_request(_request(), refs)

    assert not report.ok
    assert any(issue.code == "checksum_mismatch" for issue in report.blocking)


def test_fingerprint_is_stable_across_ref_and_profile_order() -> None:
    refs = _refs()
    profiles = [ref.profile for ref in refs if ref.profile is not None]

    first = compute_input_fingerprint(owner_id="user-1", dataset_refs=refs, profiles=profiles)
    second = compute_input_fingerprint(owner_id="user-1", dataset_refs=list(reversed(refs)), profiles=list(reversed(profiles)))

    assert first == second


def test_fingerprint_changes_when_contrast_relevant_structure_changes() -> None:
    refs = _refs()
    profiles = [ref.profile for ref in refs if ref.profile is not None]
    metadata = next(profile for profile in profiles if isinstance(profile, MetadataProfile))
    changed = metadata.model_copy(update={
        "levels": {"treatment": {"control": 2, "drought": 2}},
    })

    before = compute_input_fingerprint(owner_id="user-1", dataset_refs=refs, profiles=profiles)
    after = compute_input_fingerprint(
        owner_id="user-1",
        dataset_refs=refs,
        profiles=[changed if profile is metadata else profile for profile in profiles],
    )

    assert before != after
