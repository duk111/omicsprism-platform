from __future__ import annotations

from backend.app.analysis_specs import AnalysisSpecRegistry
from backend.app.models import AnalysisType


def test_registry_uses_existing_analysis_type_as_single_source_of_truth() -> None:
    registry = AnalysisSpecRegistry()

    assert registry.analysis_types() == (
        AnalysisType.DEG,
        AnalysisType.DEM,
        AnalysisType.GMA,
    )
    assert registry.get(AnalysisType.DEG).display_label == "DEG"
    assert registry.get(AnalysisType.DEM).display_label == "DEM"
    assert registry.get(AnalysisType.GMA).display_label == "GMA"


def test_registry_contains_input_and_parameter_rule_containers() -> None:
    registry = AnalysisSpecRegistry()

    for analysis_type in registry.analysis_types():
        spec = registry.get(analysis_type)
        assert spec.analysis_type is analysis_type
        assert isinstance(spec.input_rules, tuple)
        assert isinstance(spec.parameter_rules, tuple)


def test_registry_filters_parameters_to_analysis_whitelist() -> None:
    registry = AnalysisSpecRegistry()

    requested = registry.requested_params(AnalysisType.DEG, {
        "compare_field": "treatment",
        "tested_levels": "salt",
        "counts": "counts",
        "comparison": "treatment",
    })
    effective = registry.effective_params(AnalysisType.DEG, requested)

    assert requested == {"compare_field": "treatment", "tested_levels": "salt"}
    assert "counts" not in effective
    assert "comparison" not in effective
    assert effective["padj_cutoff"] == 0.05


def test_registry_normalizes_role_aliases_and_evaluates_capabilities() -> None:
    registry = AnalysisSpecRegistry()

    assert registry.canonical_role("metabs") == "metabolome"
    assert registry.required_roles(AnalysisType.DEM) == ("metabolome", "metadata")

    report = registry.capability_report(["metabs", "metadata"])
    dem = next(item for item in report.items if item.analysis_type == "DEM")
    assert dem.present_roles == ["metabolome", "metadata"]
    assert dem.missing_roles == []
    assert dem.next_step == "ready_for_parameter_resolution"
