from __future__ import annotations

from types import SimpleNamespace

from backend.app.agent.context import ContextAssembler
from backend.app.agent.dataset_profile import MetadataProfile, MatrixProfile
from backend.app.agent.graph import DatasetProfileRef, GraphState, JobRef, JobSummary
from backend.app.agent.param_resolver import ContrastSpec, DEGParams, ResolvedRequest, ScopeSpec


def _state() -> GraphState:
    metadata = MetadataProfile(
        role="metadata",
        columns=["sample_id", "line", "timepoint", "treatment"],
        levels={
            "line": {"WT": 2, "mutant": 2},
            "timepoint": {"24h": 4},
            "treatment": {"control": 2, "salt": 2},
        },
        sample_ids=["s1", "s2", "s3", "s4"],
        rows=[["s1", "WT", "24h", "control"]],
        alignment={"counts": "exact"},
    )
    matrix = MatrixProfile(
        role="counts",
        shape=(2, 4),
        sample_ids=["s1", "s2", "s3", "s4"],
        feature_type="gene",
        feature_id_examples=["g1", "g2"],
        numeric_type="integer_counts",
        has_negative=False,
        missing_rate=0,
    )
    return GraphState(
        thread_id="thread-1",
        user_id="user-1",
        user_message="Compare salt and control in WT",
        dataset_profiles=[
            DatasetProfileRef(
                dataset_id="metadata-1",
                owner_id="user-1",
                filename="metadata.csv",
                checksum="sha256:" + "a" * 64,
                profile=metadata,
            ),
            DatasetProfileRef(
                dataset_id="counts-1",
                owner_id="user-1",
                filename="counts.csv",
                checksum="sha256:" + "b" * 64,
                profile=matrix,
            ),
        ],
        resolved_request=ResolvedRequest(
            analysis_type="DEG",
            params=DEGParams(contrast=ContrastSpec(
                compare_field="treatment",
                tested_level="salt",
                reference_level="control",
                scope=ScopeSpec(mode="fixed", fixed_filters={"line": "WT"}),
            )),
        ),
        recent_jobs=[JobRef(job_id=f"job-{index}", owner_id="user-1") for index in range(5)],
        job_summary=JobSummary(
            job_id="job-4",
            owner_id="user-1",
            status="succeeded",
            artifacts=["differential_gene_counts.csv"],
        ),
    )


def test_context_assembler_exposes_bounded_facts_and_decisions() -> None:
    context = ContextAssembler().assemble(_state())

    assert context.fact_index.metadata_fields == ["sample_id", "line", "timepoint", "treatment"]
    assert context.fact_index.metadata_levels["line"] == {"WT": 2, "mutant": 2}
    assert context.fact_index.sample_count == 4
    assert context.fact_index.alignment == {"counts": "exact"}
    assert context.fact_index.job_artifacts == {
        "job-4": ["differential_gene_counts.csv"]
    }
    assert context.decision_ledger.compare_field == "treatment"
    assert context.decision_ledger.scope == ScopeSpec(
        mode="fixed", fixed_filters={"line": "WT"}
    )
    assert all("rows" not in item for item in context.model_dump().values() if isinstance(item, dict))


def test_context_assembler_limits_working_set_and_marks_truncation() -> None:
    context = ContextAssembler().assemble(_state())

    assert len(context.working_set.items) <= 3
    assert context.working_set.truncated
    assert context.fact_index.context_version.startswith("facts.v1:")
    assert context.decision_ledger.context_version.startswith("ledger.v1:")
    assert context.working_set.context_version.startswith("working.v1:")


def test_context_assembler_does_not_accept_unbounded_payloads() -> None:
    state = SimpleNamespace(
        user_message="inspect",
        dataset_profiles=[],
        recent_jobs=[],
        conversation_summary="x" * 5000,
    )

    context = ContextAssembler().assemble(state)

    assert len(context.conversation_summary or "") == 1200
    assert context.fact_index.metadata_fields == []


def test_context_assembler_truncates_large_metadata_index() -> None:
    profile = MetadataProfile(
        role="metadata",
        columns=["sample_id", *[f"factor_{index}" for index in range(25)]],
        levels={f"factor_{index}": {"value": 1} for index in range(25)},
        sample_ids=["s1"],
        rows=None,
        alignment={},
    )
    state = SimpleNamespace(
        user_message="inspect",
        dataset_profiles=[SimpleNamespace(profile=profile)],
        recent_jobs=[],
    )

    context = ContextAssembler().assemble(state)

    assert len(context.fact_index.metadata_fields) == 20
    assert len(context.fact_index.metadata_levels) == 19
    assert context.fact_index.truncated
