from __future__ import annotations

from backend.app.analysis_specs import AnalysisSpecRegistry
from backend.app.models import AnalysisType


def test_registry_uses_existing_analysis_type_as_single_source_of_truth() -> None:
    registry = AnalysisSpecRegistry()

    assert registry.analysis_types() == (
        AnalysisType.DIFFERENTIAL,
        AnalysisType.DEM,
        AnalysisType.CORRELATION,
    )
    assert registry.get(AnalysisType.DIFFERENTIAL).display_label == "DEG"
    assert registry.get(AnalysisType.DEM).display_label == "DEM"
    assert registry.get(AnalysisType.CORRELATION).display_label == "GMA"


def test_registry_contains_input_and_parameter_rule_containers() -> None:
    registry = AnalysisSpecRegistry()

    for analysis_type in registry.analysis_types():
        spec = registry.get(analysis_type)
        assert spec.analysis_type is analysis_type
        assert isinstance(spec.input_rules, tuple)
        assert isinstance(spec.parameter_rules, tuple)
