from __future__ import annotations

import json
from types import SimpleNamespace

from backend.app.agent.context import (
    ContextAssembler,
    RecentMessage,
    RecentMessages,
    build_recent_messages,
)
from backend.app.agent.dataset_profile import MetadataProfile, MatrixProfile
from backend.app.agent.graph import DatasetProfileRef, GraphState, JobRef, JobSummary
from backend.app.agent.param_resolver import ContrastSpec, DEGParams, ResolvedRequest, ScopeSpec
from backend.app.agent.schemas import RunFocus


def _state() -> GraphState:
    metadata = MetadataProfile(
        role="metadata",
        columns=["line", "timepoint", "treatment"],
        levels={
            "line": {"WT": 2, "mutant": 2},
            "timepoint": {"24h": 4},
            "treatment": {"control": 2, "salt": 2},
        },
        sample_ids=["s1", "s2", "s3", "s4"],
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

    assert context.fact_index.metadata_fields == ["line", "timepoint", "treatment"]
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


def test_context_assembler_builds_narrow_agent_projections() -> None:
    state = _state().model_copy(update={
        "current_job": JobRef(job_id="job-4", owner_id="user-1"),
        "focus": RunFocus(
            in_scope_job_ids=["job-4"],
            resolved_entities={},
            last_citation=None,
        ),
        "recent_messages": RecentMessages(
            context_version="messages.v1:test",
            messages=[
                RecentMessage(
                    role="user" if index % 2 == 0 else "assistant",
                    turn_id=f"turn-{index}",
                    text=f"message {index}",
                )
                for index in range(8)
            ],
        ),
    })
    assembler = ContextAssembler()

    qa = assembler.assemble_for_qa(state)
    analysis = assembler.assemble_for_analysis(state)
    result_qa = assembler.assemble_for_result_qa(state)
    main = assembler.assemble(state)

    assert qa.fact_index.dataset_roles == ["metadata", "counts"]
    qa_payload = qa.model_dump(mode="json")
    assert set(qa_payload) == {"user_message", "recent_messages", "fact_index"}
    assert set(qa_payload["fact_index"]) == {"dataset_roles"}

    analysis_payload = analysis.model_dump(mode="json")
    assert set(analysis_payload) == {"fact_index", "pending_analysis", "decision_ledger"}
    assert analysis.fact_index.metadata_fields == ["line", "timepoint", "treatment"]
    assert analysis.fact_index.metadata_levels["treatment"] == {"control": 2, "salt": 2}

    result_payload = result_qa.model_dump(mode="json")
    assert result_qa.current_job is not None
    assert result_qa.current_job.job_id == "job-4"
    assert result_qa.focus.in_scope_job_ids == ["job-4"]
    assert result_qa.job_artifacts == {
        "job-4": ["differential_gene_counts.csv"]
    }
    assert "metadata_fields" not in json.dumps(result_payload)
    assert "metadata_levels" not in json.dumps(result_payload)

    qa_size = len(json.dumps(qa_payload, ensure_ascii=False))
    main_size = len(json.dumps(main.model_dump(mode="json"), ensure_ascii=False))
    assert qa_size < main_size


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
        columns=[*[f"factor_{index}" for index in range(25)]],
        levels={f"factor_{index}": {"value": 1} for index in range(25)},
        sample_ids=["s1"],
        alignment={},
    )
    state = SimpleNamespace(
        user_message="inspect",
        dataset_profiles=[SimpleNamespace(profile=profile)],
        recent_jobs=[],
    )

    context = ContextAssembler().assemble(state)

    assert len(context.fact_index.metadata_fields) == 20
    assert len(context.fact_index.metadata_levels) == 20
    assert context.fact_index.truncated


def test_recent_messages_are_bounded_and_compacted_deterministically() -> None:
    records = [
        SimpleNamespace(
            message_id=f"turn-{index}",
            role="user" if index % 2 == 0 else "assistant",
            blocks=[SimpleNamespace(text=f"message {index}")],
        )
        for index in range(12)
    ]

    recent, summary = build_recent_messages(records)

    assert len(recent.messages) == 8
    assert recent.truncated
    assert summary is not None
    assert "message 0" in summary
    assert "message 11" not in summary
    assert recent.messages[-1].text == "message 11"
