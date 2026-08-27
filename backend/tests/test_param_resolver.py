from __future__ import annotations

import pytest

from backend.app.agent.dataset_profile import MetadataProfile
from backend.app.agent.param_resolver import (
    AnalysisProposal,
    ContrastSpec,
    DEGParams,
    ScopeSpec,
    resolve_analysis_request,
)


def _metadata(rows: list[list[str]], columns: list[str]) -> MetadataProfile:
    levels: dict[str, dict[str, int]] = {}
    for index, column in enumerate(columns):
        if column == "sample_id":
            continue
        counts: dict[str, int] = {}
        for row in rows:
            counts[row[index]] = counts.get(row[index], 0) + 1
        levels[column] = counts
    return MetadataProfile(
        role="metadata",
        columns=columns,
        levels=levels,
        sample_ids=[row[0] for row in rows],
        rows=rows,
        alignment={},
    )


def _two_level(*, reference: str = "control", tested: str = "salt", tested_count: int = 2) -> MetadataProfile:
    rows = [["s1", reference], ["s2", reference]]
    rows.extend([[f"s{index + 3}", tested] for index in range(tested_count)])
    return _metadata(rows, ["sample_id", "treatment"])


def _proposal(**updates: object) -> AnalysisProposal:
    values: dict[str, object] = {
        "analysis_type": "DEG",
        "scope": ScopeSpec(mode="all"),
    }
    values.update(updates)
    return AnalysisProposal.model_validate(values)


def test_scope_spec_enforces_disjoint_modes() -> None:
    assert ScopeSpec(mode="all") == ScopeSpec(mode="all")
    assert ScopeSpec(mode="unknown") == ScopeSpec(mode="unknown")
    assert ScopeSpec(mode="fixed", fixed_filters={"genotype": "WT"})
    assert ScopeSpec(mode="stratified", blocking_fields=["timepoint"])
    with pytest.raises(ValueError, match="fixed scope"):
        ScopeSpec(mode="fixed")
    with pytest.raises(ValueError, match="stratified scope"):
        ScopeSpec(mode="stratified")
    with pytest.raises(ValueError, match="cannot carry"):
        ScopeSpec(mode="all", blocking_fields=["timepoint"])
    with pytest.raises(ValueError, match="duplicates"):
        ScopeSpec(mode="stratified", blocking_fields=["timepoint", "timepoint"])


def test_two_groups_with_explicit_reference_are_resolved() -> None:
    result = resolve_analysis_request(
        "",
        [_two_level(reference="baseline", tested="treated")],
        _proposal(compare_field="treatment", reference_level="baseline"),
    )

    assert result.missing == []
    assert result.params is not None
    assert result.params.contrast == ContrastSpec(
        compare_field="treatment", tested_level="treated", reference_level="baseline"
    )


def test_control_marker_is_used_only_when_it_is_unique() -> None:
    resolved = resolve_analysis_request(
        "", [_two_level()], _proposal(compare_field="treatment")
    )
    ambiguous = resolve_analysis_request(
        "",
        [_two_level(reference="baseline", tested="vehicle")],
        _proposal(compare_field="treatment"),
    )

    assert resolved.params is not None
    assert resolved.params.contrast.reference_level == "control"
    assert ambiguous.params is None
    assert ambiguous.missing[0].field == "reference_level"


def test_three_levels_use_explicit_user_comparison_without_guessing() -> None:
    profile = _metadata(
        [["s1", "control"], ["s2", "control"],
         ["s3", "salt"], ["s4", "salt"],
         ["s5", "drought"], ["s6", "drought"]],
        ["sample_id", "condition"],
    )

    explicit = resolve_analysis_request(
        "compare salt and control", [profile], _proposal()
    )
    unspecified = resolve_analysis_request(
        "analyze treatment response", [profile], _proposal(compare_field="condition")
    )

    assert explicit.params is not None
    assert explicit.params.contrast.tested_level == "salt"
    assert explicit.params.contrast.reference_level == "control"
    assert unspecified.params is None
    assert unspecified.missing[0].field == "tested_level"
    assert set(unspecified.missing[0].options) == {"control", "salt", "drought"}


def test_genotype_and_timepoint_constraints_are_resolved_from_real_rows() -> None:
    rows: list[list[str]] = []
    sample = 1
    for genotype in ("WT", "mutant"):
        for timepoint in ("0h", "24h"):
            for treatment in ("control", "salt"):
                for _ in range(2):
                    rows.append([f"s{sample}", genotype, timepoint, treatment])
                    sample += 1
    profile = _metadata(rows, ["sample_id", "genotype", "timepoint", "treatment"])

    result = resolve_analysis_request(
        "在 WT 的 24h 比较 salt 和 control",
        [profile],
        _proposal(scope=ScopeSpec(
            mode="fixed",
            fixed_filters={"genotype": "WT", "timepoint": "24h"},
        )),
    )

    assert result.params is not None
    assert result.params.contrast.compare_field == "treatment"
    assert result.params.contrast.scope == ScopeSpec(
        mode="fixed",
        fixed_filters={"genotype": "WT", "timepoint": "24h"},
    )


def test_unbounded_secondary_factor_returns_all_legal_candidates_for_clarification() -> None:
    rows: list[list[str]] = []
    for timepoint in ("0h", "24h"):
        rows.extend([
            [f"{timepoint}-c1", timepoint, "control"],
            [f"{timepoint}-c2", timepoint, "control"],
            [f"{timepoint}-s1", timepoint, "salt"],
            [f"{timepoint}-s2", timepoint, "salt"],
        ])
    profile = _metadata(rows, ["sample_id", "timepoint", "treatment"])

    result = resolve_analysis_request(
        "compare salt and control",
        [profile],
        _proposal(compare_field="treatment", scope=ScopeSpec(mode="unknown")),
    )

    assert result.params is None
    assert result.missing[0].field == "scope"
    assert set(result.missing[0].options) == {"all", "stratified", "fixed"}


def test_invalid_facts_and_replicates_are_rejected_without_fuzzy_correction() -> None:
    profile = _two_level(tested_count=1)
    insufficient = resolve_analysis_request(
        "",
        [profile],
        _proposal(compare_field="treatment", tested_level="salt", reference_level="control"),
    )
    unknown_column = resolve_analysis_request(
        "",
        [profile],
        _proposal(compare_field="condition", tested_level="salt", reference_level="control"),
    )
    unknown_level = resolve_analysis_request(
        "",
        [_two_level()],
        _proposal(compare_field="treatment", tested_level="salty", reference_level="control"),
    )

    assert "min_replicates" in insufficient.missing[0].reason
    assert unknown_column.missing[0].field == "compare_field"
    assert "salty" in unknown_level.missing[0].reason


def test_two_legal_compare_fields_require_clarification() -> None:
    rows: list[list[str]] = []
    sample = 1
    for genotype in ("WT", "mutant"):
        for treatment in ("control", "salt"):
            for _ in range(2):
                rows.append([f"s{sample}", genotype, treatment])
                sample += 1
    result = resolve_analysis_request(
        "analyze the experiment",
        [_metadata(rows, ["sample_id", "genotype", "treatment"])],
        _proposal(),
    )

    assert result.params is None
    assert result.missing[0].field == "contrast"
    assert len(result.missing[0].options) == 2


def test_new_user_semantics_override_conflicting_prior_contrast() -> None:
    profile = _metadata(
        [["s1", "control"], ["s2", "control"],
         ["s3", "salt"], ["s4", "salt"],
         ["s5", "drought"], ["s6", "drought"]],
        ["sample_id", "condition"],
    )
    prior = DEGParams(contrast=ContrastSpec(
        compare_field="condition", tested_level="salt", reference_level="control"
    ))

    result = resolve_analysis_request(
        "compare drought and control", [profile], _proposal(), prior_params=prior
    )

    assert result.params is not None
    assert result.params.contrast.tested_level == "drought"


def test_same_field_value_and_typed_min_replicates_are_preserved() -> None:
    rows = [
        ["s1", "WT", "control"], ["s2", "WT", "control"],
        ["s3", "WT", "salt"], ["s4", "WT", "salt"],
    ]
    result = resolve_analysis_request(
        "",
        [_metadata(rows, ["sample_id", "genotype", "treatment"])],
        _proposal(
            compare_field="treatment",
            tested_level="salt",
            reference_level="control",
            scope=ScopeSpec(mode="fixed", fixed_filters={"genotype": "WT"}),
            requested_params={"min_replicates": 2},
        ),
    )

    assert result.params is not None
    assert result.params.min_replicates == 2
    assert result.params.contrast.scope == ScopeSpec(
        mode="fixed", fixed_filters={"genotype": "WT"}
    )


def test_legacy_same_fields_column_keeps_all_valid_strata() -> None:
    rows: list[list[str]] = []
    sample = 1
    for batch in ("b1", "b2"):
        for treatment in ("control", "salt"):
            for _ in range(2):
                rows.append([f"s{sample}", batch, treatment])
                sample += 1
    proposal = AnalysisProposal.from_legacy(
        "deg",
        {
            "compare_field": "treatment",
            "tested_levels": "salt",
            "reference_level": "control",
            "same_fields": "batch",
            "min_replicates": 2,
        },
    )

    result = resolve_analysis_request(
        "",
        [_metadata(rows, ["sample_id", "batch", "treatment"])],
        proposal,
    )

    assert result.params is not None
    assert result.params.contrast.scope == ScopeSpec(
        mode="stratified", blocking_fields=["batch"]
    )
    assert result.legacy_params()["same_fields"] == "batch"
