from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from backend.app.agent.approvals import (
    ApprovalExpired,
    ApprovalMismatch,
    ApprovalNotFound,
    InMemoryApprovalGate,
    JsonApprovalGate,
)
from backend.app.agent.plans import compute_plan_hash
from backend.app.agent.product_store import (
    AgentResourceNotFound,
    IdempotencyConflict,
    InMemoryAgentProductStore,
)
from backend.app.agent.schemas import (
    AgentDecision,
    AgentInputBundleRecord,
    AgentInputSourceRef,
    AgentApprovalRequest,
    AgentMessageRecord,
    AgentThreadCreateRequest,
    AgentThreadRecord,
    AgentTurnCreateRequest,
    AgentTurnRecord,
    PlanRecord,
)
from backend.app.models import AnalysisType


def _thread() -> AgentThreadRecord:
    now = datetime.now(timezone.utc)
    return AgentThreadRecord(
        thread_id="thread-1",
        user_id="user-1",
        title="Salt stress analysis",
        current_run_id="run-1",
        status="active",
        version=0,
        created_at=now,
        updated_at=now,
    )


def _turn(*, request_hash: str = "sha256:request-a") -> AgentTurnRecord:
    now = datetime.now(timezone.utc)
    return AgentTurnRecord(
        turn_id="turn-1",
        thread_id="thread-1",
        run_id="run-1",
        user_id="user-1",
        idempotency_key="turn-key-1",
        request_hash=request_hash,
        status="queued",
        attempt=0,
        lease_owner=None,
        lease_expires_at=None,
        error_code=None,
        created_at=now,
        updated_at=now,
        started_at=None,
        completed_at=None,
    )


def _plan(source: AgentInputSourceRef) -> PlanRecord:
    plan = PlanRecord(
        plan_id="plan-1",
        run_id="run-1",
        thread_id="thread-1",
        user_id="user-1",
        analysis_type=AnalysisType.DIFFERENTIAL,
        input_source=source,
        requested_params={"compare_field": "treatment"},
        effective_params={"compare_field": "treatment"},
        contrasts=[{
            "compare_field": "treatment",
            "tested_level": "salt",
            "reference_level": "control",
        }],
        plan_hash="pending",
        approval_id=None,
    )
    plan.plan_hash = compute_plan_hash(plan)
    return plan


@pytest.mark.parametrize(("schema", "payload"), [
    (AgentThreadCreateRequest, {"focus_job_ids": []}),
    (AgentTurnCreateRequest, {"message": "analyze these files", "focus_job_ids": []}),
    (AgentApprovalRequest, {"decision": "approve", "plan_hash": "sha256:plan"}),
])
def test_agent_http_requests_reject_client_supplied_user_id(schema, payload) -> None:
    payload = {**payload, "user_id": "attacker"}

    with pytest.raises(ValidationError):
        schema.model_validate(payload)


def test_grounded_answer_in_agent_decision_is_bounded() -> None:
    payload = {
        "action": "answer",
        "reasoning_summary": "Answer from returned evidence",
        "feasibility": None,
        "analysis_recommendations": [],
        "requires_approval": False,
        "requested_params": {},
        "grounded_answer": {
            "claims": [{
                "text": "GeneA has PearsonR 0.71",
                "citation": {
                    "artifact": "T02_High_Confidence_Network.csv",
                    "checksum": "sha256:fixture",
                    "row_ids": [7],
                },
            }],
        },
    }
    assert AgentDecision.model_validate(payload).grounded_answer is not None

    payload["grounded_answer"]["claims"][0]["text"] = "x" * 1001
    with pytest.raises(ValidationError):
        AgentDecision.model_validate(payload)


def test_plan_hash_binds_real_input_source_ref() -> None:
    existing = _plan(AgentInputSourceRef(kind="existing_job", source_id="job-1"))
    staged = _plan(AgentInputSourceRef(kind="staged_bundle", source_id="bundle-1"))

    assert existing.plan_hash == compute_plan_hash(existing)
    assert staged.plan_hash == compute_plan_hash(staged)
    assert existing.plan_hash != staged.plan_hash


def test_product_store_hides_cross_user_resources_as_not_found() -> None:
    store = InMemoryAgentProductStore()
    store.save_thread(_thread())
    store.append_message(AgentMessageRecord(
        message_id="message-1",
        thread_id="thread-1",
        run_id="run-1",
        user_id="user-1",
        role="user",
        blocks=[{"type": "text", "text": "analyze these files"}],
        created_at=datetime.now(timezone.utc),
    ))
    store.create_turn(_turn())
    store.save_input_bundle(AgentInputBundleRecord(
        bundle_id="bundle-1",
        thread_id="thread-1",
        user_id="user-1",
        status="active",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        created_at=datetime.now(timezone.utc),
    ))

    with pytest.raises(AgentResourceNotFound):
        store.get_thread(thread_id="thread-1", user_id="user-2")
    with pytest.raises(AgentResourceNotFound):
        store.list_messages(thread_id="thread-1", user_id="user-2")
    with pytest.raises(AgentResourceNotFound):
        store.get_turn(turn_id="turn-1", user_id="user-2")
    with pytest.raises(AgentResourceNotFound):
        store.get_input_bundle(bundle_id="bundle-1", user_id="user-2")


def test_turn_idempotency_reuses_same_request_and_rejects_changed_request() -> None:
    store = InMemoryAgentProductStore()
    store.save_thread(_thread())

    first = store.create_turn(_turn())
    replay = store.create_turn(_turn())

    assert replay.turn_id == first.turn_id
    assert len(store.list_turns(thread_id="thread-1", user_id="user-1")) == 1

    with pytest.raises(IdempotencyConflict):
        store.create_turn(_turn(request_hash="sha256:request-b"))


def test_turn_idempotency_key_cannot_cross_thread_even_with_same_hash() -> None:
    store = InMemoryAgentProductStore()
    store.save_thread(_thread())
    store.create_turn(_turn())
    other_thread = _thread().model_copy(update={"thread_id": "thread-2", "current_run_id": "run-2"})
    store.save_thread(other_thread)

    with pytest.raises(IdempotencyConflict):
        store.create_turn(_turn().model_copy(update={"thread_id": "thread-2", "run_id": "run-2"}))


def test_approval_cross_user_is_indistinguishable_from_missing() -> None:
    now = datetime.now(timezone.utc)
    gate = InMemoryApprovalGate()
    approval_id = gate.suspend(
        run_id="run-1",
        user_id="user-1",
        plan_hash="sha256:plan",
        expires_at=now + timedelta(minutes=10),
    )

    with pytest.raises(ApprovalNotFound):
        gate.resume(
            approval_id=approval_id,
            run_id="run-1",
            user_id="user-2",
            plan_hash="sha256:plan",
            now=now,
        )


@pytest.mark.parametrize("storage", ["memory", "json"])
def test_rejected_approval_cannot_be_resumed(storage: str, tmp_path) -> None:
    now = datetime.now(timezone.utc)
    gate = InMemoryApprovalGate() if storage == "memory" else JsonApprovalGate(tmp_path)
    approval_id = gate.suspend(
        run_id="run-1",
        user_id="user-1",
        plan_hash="sha256:plan",
        expires_at=now + timedelta(minutes=10),
    )
    gate.reject(
        approval_id=approval_id,
        run_id="run-1",
        user_id="user-1",
        plan_hash="sha256:plan",
        now=now,
    )

    assert not gate.is_valid(
        approval_id=approval_id,
        run_id="run-1",
        user_id="user-1",
        plan_hash="sha256:plan",
        now=now,
    )
    with pytest.raises(ApprovalMismatch):
        gate.resume(
            approval_id=approval_id,
            run_id="run-1",
            user_id="user-1",
            plan_hash="sha256:plan",
            now=now,
        )


@pytest.mark.parametrize("storage", ["memory", "json"])
def test_expired_approval_cannot_be_resumed(storage: str, tmp_path) -> None:
    now = datetime.now(timezone.utc)
    gate = InMemoryApprovalGate() if storage == "memory" else JsonApprovalGate(tmp_path)
    approval_id = gate.suspend(
        run_id="run-1",
        user_id="user-1",
        plan_hash="sha256:plan",
        expires_at=now + timedelta(minutes=10),
    )

    with pytest.raises(ApprovalExpired):
        gate.resume(
            approval_id=approval_id,
            run_id="run-1",
            user_id="user-1",
            plan_hash="sha256:plan",
            now=now + timedelta(minutes=11),
        )

    with pytest.raises(ApprovalExpired):
        gate.resume(
            approval_id=approval_id,
            run_id="run-1",
            user_id="user-1",
            plan_hash="sha256:plan",
            now=now,
        )
