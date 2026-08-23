from __future__ import annotations

from backend.app.agent.dataset_profile import MatrixProfile, MetadataProfile
from backend.app.agent.tools import AgentInputFile, AgentToolRuntime


def _runtime(*items: tuple[str, str, bytes]) -> AgentToolRuntime:
    return AgentToolRuntime(
        user_id="user-1",
        inputs={field: AgentInputFile(filename, content) for field, filename, content in items},
    )


def test_integer_counts_profile_preserves_shape_and_feature_examples() -> None:
    profile = _runtime((
        "counts",
        "counts.csv",
        b"gene,s1,s2\nAT1G00001,10,12\nAT1G00002,8,9\n",
    )).inspect_dataset()[0]

    assert isinstance(profile, MatrixProfile)
    assert profile.role == "counts"
    assert profile.shape == (2, 2)
    assert profile.sample_ids == ["s1", "s2"]
    assert profile.numeric_type == "integer_counts"
    assert profile.feature_type == "gene"
    assert profile.feature_id_examples == ["AT1G00001", "AT1G00002"]


def test_continuous_abundance_profile_is_not_classified_as_counts() -> None:
    profile = _runtime((
        "metabs",
        "metabs.csv",
        b"metabolite,s1,s2\nM1,0.25,1.5\nM2,2.1,3.7\n",
    )).inspect_dataset()[0]

    assert isinstance(profile, MatrixProfile)
    assert profile.role == "metabs"
    assert profile.numeric_type == "continuous_abundance"
    assert profile.feature_type == "metabolite"
    assert profile.has_negative is False


def test_missing_values_and_mixed_cells_are_exposed_deterministically() -> None:
    profile = _runtime((
        "counts",
        "counts.csv",
        b"gene,s1,s2,s3\ng1,1,,3\ng2,4,not-a-number,6\n",
    )).inspect_dataset()[0]

    assert isinstance(profile, MatrixProfile)
    assert profile.numeric_type == "mixed"
    assert profile.missing_rate == 1 / 6
    assert profile.has_negative is False


def test_metadata_profile_keeps_multiple_factors_and_levels() -> None:
    profile = _runtime((
        "metadata",
        "metadata.csv",
        b"sample_id,treatment,batch\ns1,control,b1\ns2,control,b2\ns3,salt,b1\ns4,salt,b2\n",
    )).inspect_dataset()[0]

    assert isinstance(profile, MetadataProfile)
    assert profile.role == "metadata"
    assert profile.columns == ["sample_id", "treatment", "batch"]
    assert profile.levels["treatment"] == {"control": 2, "salt": 2}
    assert profile.levels["batch"] == {"b1": 2, "b2": 2}
    assert profile.rows == [
        ["s1", "control", "b1"],
        ["s2", "control", "b2"],
        ["s3", "salt", "b1"],
        ["s4", "salt", "b2"],
    ]


def test_exact_alignment_is_reported_between_matrix_and_metadata() -> None:
    profiles = _runtime(
        (
            "counts",
            "counts.csv",
            b"gene,s1,s2\ng1,1,2\n",
        ),
        (
            "metadata",
            "metadata.csv",
            b"sample_id,condition\ns1,control\ns2,salt\n",
        ),
    ).inspect_dataset()

    metadata = next(profile for profile in profiles if isinstance(profile, MetadataProfile))
    assert metadata.alignment == {"counts": "exact"}


def test_sample_mismatch_is_not_silently_treated_as_exact() -> None:
    profiles = _runtime(
        (
            "counts",
            "counts.csv",
            b"gene,s1,s2,s3\ng1,1,2,3\n",
        ),
        (
            "metadata",
            "metadata.csv",
            b"sample_id,condition\ns1,control\ns4,salt\n",
        ),
    ).inspect_dataset()

    metadata = next(profile for profile in profiles if isinstance(profile, MetadataProfile))
    assert metadata.alignment == {"counts": "mismatch"}


def test_large_metadata_omits_raw_rows_but_keeps_bounded_facts() -> None:
    rows = "".join(f"s{index},salt\n" for index in range(61))
    profile = _runtime((
        "metadata",
        "metadata.csv",
        ("sample_id,condition\n" + rows).encode(),
    )).inspect_dataset()[0]

    assert isinstance(profile, MetadataProfile)
    assert profile.rows is None
    assert profile.levels["condition"] == {"salt": 61}
    assert len(profile.sample_ids) == 61


def test_feature_examples_are_limited_to_fifteen_items() -> None:
    rows = "".join(f"AT1G{index:05d},1,2\n" for index in range(30))
    profile = _runtime((
        "counts",
        "counts.csv",
        ("gene,s1,s2\n" + rows).encode(),
    )).inspect_dataset()[0]

    assert isinstance(profile, MatrixProfile)
    assert len(profile.feature_id_examples) <= 15
    assert profile.feature_id_examples == [
        "AT1G00000",
        "AT1G00001",
        "AT1G00002",
        "AT1G00003",
        "AT1G00004",
        "AT1G00005",
        "AT1G00006",
        "AT1G00007",
        "AT1G00008",
        "AT1G00009",
        "AT1G00015",
        "AT1G00022",
        "AT1G00026",
        "AT1G00029",
    ]
