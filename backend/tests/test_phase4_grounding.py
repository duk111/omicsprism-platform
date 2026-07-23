from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.app.agent.audit import InMemoryAgentEventStore, UnsafeTracePayload
from backend.app.agent.grounding import (
    NO_EVIDENCE_TEXT,
    EvidenceGrounder,
    EvidenceGroundingError,
    GroundedAnswerPipeline,
)
from backend.app.agent.schemas import (
    ActiveProfile,
    AgentEvent,
    AgentState,
    Citation,
    GroundedAnswer,
    GroundedClaim,
    RunFocus,
    RunState,
    RunStatus,
    ToolName,
    ToolResult,
)
from backend.app.agent.store import InMemoryStateStore
from backend.app.agent.verifier import AnswerVerifier


def _evidence(*, artifact: str = "outputs/T02_High_Confidence_Network.csv", rows=None) -> ToolResult:
    return ToolResult(
        tool=ToolName.QUERY_RESULT_EVIDENCE,
        ok=True,
        rows=rows if rows is not None else [
            {"_row_id": 7, "Source": "GeneA", "Target": "M0123", "EdgeWeight": "0.82", "PearsonR": "0.71"},
        ],
        truncated=False,
        row_count=1,
        artifact=artifact,
        checksum="sha256:evidence",
        filters={},
        sort="EdgeWeight desc",
        error_code=None,
    )


def _answer(text: str, row_ids: list[int] = [7]) -> GroundedAnswer:
    return GroundedAnswer(claims=[GroundedClaim(
        text=text,
        citation=Citation(
            artifact="outputs/T02_High_Confidence_Network.csv",
            checksum="sha256:evidence",
            row_ids=row_ids,
        ),
    )])


def _state() -> RunState:
    return RunState(
        run_id="run-1", user_id="user-1", thread_id="thread-1",
        active_profile=ActiveProfile.INTERPRETATION, state=AgentState.ANSWER_WITH_EVIDENCE,
        step_no=1, plan_id=None, plan_hash=None, pending_approval_id=None,
        focus=RunFocus(in_scope_job_ids=["job-1"], resolved_entities={}, last_citation=None),
        model_calls=0, tool_calls=1, status=RunStatus.RUNNING, version=1,
    )


def test_empty_evidence_uses_the_fixed_no_evidence_template() -> None:
    answer = EvidenceGrounder().ground(_evidence(rows=[]))

    assert [claim.text for claim in answer.claims] == [NO_EVIDENCE_TEXT]
    assert answer.claims[0].citation.row_ids == []
    assert AnswerVerifier().verify(answer, [_evidence(rows=[])]).verdict.value == "approved"


def test_grounder_assigns_evidence_citations_and_updates_focus() -> None:
    evidence = _evidence()
    state = _state()

    answer = EvidenceGrounder().ground(evidence)
    EvidenceGrounder().update_focus(state, answer)

    assert answer.claims[0].citation.row_ids == [7]
    assert answer.claims[0].citation.checksum == evidence.checksum
    assert state.focus.last_citation == answer.claims[0].citation


def test_grounder_rejects_direction_claims_from_union_tables() -> None:
    evidence = _evidence(artifact="union_significant_genes.csv")

    with pytest.raises(EvidenceGroundingError, match="union"):
        EvidenceGrounder().ground(evidence, _answer("GeneA is upregulated"))


def test_verifier_rejects_invalid_citation_numbers_and_causal_claims() -> None:
    verifier = AnswerVerifier()
    evidence = _evidence()

    numeric = verifier.verify(_answer("GeneA has PearsonR 0.99"), [evidence])
    missing_row = verifier.verify(_answer("GeneA has PearsonR 0.71", [99]), [evidence])
    causal = verifier.verify(_answer("GeneA causes M0123"), [evidence])

    assert numeric.verdict.value == "rejected"
    assert missing_row.verdict.value == "rejected"
    assert causal.verdict.value == "rejected"


def test_pipeline_repairs_once_then_falls_back_to_raw_evidence() -> None:
    evidence = _evidence()
    pipeline = GroundedAnswerPipeline()
    calls = []

    repaired = pipeline.answer(
        evidence,
        _answer("GeneA has PearsonR 0.99"),
        repair=lambda _draft, _verdict: calls.append("repair") or _answer("GeneA has PearsonR 0.71"),
    )
    assert calls == ["repair"]
    assert repaired.claims[0].text == "GeneA has PearsonR 0.71"

    fallback = pipeline.answer(
        evidence,
        _answer("GeneA causes M0123"),
        repair=lambda _draft, _verdict: _answer("GeneA causes M0123"),
    )
    assert fallback.claims[0].text.startswith("验证未通过")
    assert fallback.claims[1].citation.row_ids == [7]


def test_followup_keeps_interpretation_context_and_rerun_returns_to_analysis() -> None:
    from backend.app.agent.runtime import FixtureRunCoordinator
    from backend.app.agent.model import ScriptedModelAdapter
    from backend.app.agent.approvals import InMemoryApprovalGate

    state = _state().model_copy(update={"state": AgentState.AWAIT_FOLLOWUP})
    store = InMemoryStateStore()
    coordinator = FixtureRunCoordinator.create(
        state_store=store,
        model=ScriptedModelAdapter([]),
        initial_state=state,
        approval_gate=InMemoryApprovalGate(),
    )

    followup = coordinator.run_step(run_id="run-1", user_id="user-1", user_message="继续解释这个结果")
    assert followup.active_profile is ActiveProfile.INTERPRETATION
    assert followup.state is AgentState.ANSWER_WITH_EVIDENCE
    assert followup.focus.in_scope_job_ids == ["job-1"]

    rerun = coordinator.run_step(run_id="run-1", user_id="user-1", user_message="按新参数重跑")
    assert rerun.active_profile is ActiveProfile.ANALYSIS
    assert rerun.state is AgentState.CHECK_INPUTS


def test_trace_store_is_append_only_user_bound_and_redacts_unsafe_payloads() -> None:
    store = InMemoryAgentEventStore()
    event = AgentEvent(
        event_id="event-1", run_id="run-1", user_id="user-1", step_no=1,
        event_type="route.decided", payload={"intent": "interpret"},
    )
    store.append(event)

    assert store.list_for_run(run_id="run-1", user_id="user-1") == [event]
    assert store.list_for_run(run_id="run-1", user_id="user-2") == []
    with pytest.raises(UnsafeTracePayload):
        store.append(event.model_copy(update={
            "event_id": "event-2",
            "payload": {"database_url": "postgresql://secret"},
        }))
