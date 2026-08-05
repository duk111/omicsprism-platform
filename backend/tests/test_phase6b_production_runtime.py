from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from backend.app.agent.approvals import InMemoryApprovalGate
from backend.app.agent.audit import InMemoryAgentEventStore
from backend.app.agent.model import ModelAdapter
from backend.app.agent.plans import InMemoryPlanStore, compute_plan_hash
from backend.app.agent.runtime import CoordinatorBudgetExceeded, ProductionRunCoordinator
from backend.app.agent.schemas import (
    ActiveProfile,
    AgentAction,
    AgentDecision,
    AgentState,
    AgentTurnRecord,
    Citation,
    Feasibility,
    FeasibilityVerdict,
    GroundedAnswer,
    GroundedClaim,
    ModelContext,
    PlanRecord,
    RunFocus,
    RunState,
    RunStatus,
)
from backend.app.agent.store import InMemoryStateStore, StateConflict
from backend.app.agent.tools import AgentInputFile, AgentToolRuntime
from backend.app.models import (
    AnalysisType,
    FileArtifactInfo,
    FileArtifactKind,
    JobRecord,
    JobStatus,
)


class _RecordingModel(ModelAdapter):
    def __init__(self, decisions: list[AgentDecision]) -> None:
        self.decisions = list(decisions)
        self.contexts: list[ModelContext] = []

    def decide(self, context: ModelContext) -> AgentDecision:
        self.contexts.append(context.model_copy(deep=True))
        return self.decisions.pop(0).model_copy(deep=True)


class _Jobs:
    def __init__(self, *jobs: JobRecord) -> None:
        self.jobs = {job.id: job for job in jobs}
        self.saved: list[JobRecord] = []

    def get_for_user(self, job_id: str, user_id: str) -> JobRecord:
        job = self.jobs.get(job_id)
        if job is None or job.owner_id != user_id:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    def save(self, job: JobRecord) -> None:
        self.jobs[job.id] = job
        self.saved.append(job)


class _Files:
    def __init__(self, result_text: str = "") -> None:
        self.result_text = result_text

    def copy_input_artifact(self, _source_job_id, _target_job_id, source):
        return source

    def read_artifact_text(self, _job_id: str, _relative_path: str, *, max_chars=None) -> str:
        return self.result_text

    def recent_log(self, _job_id: str):
        return None, None


class _Executor:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    def enqueue(self, job_id: str) -> None:
        self.enqueued.append(job_id)


def _state(*, profile=ActiveProfile.ANALYSIS, state=AgentState.CHECK_INPUTS, focus=None) -> RunState:
    return RunState(
        run_id="run-1",
        user_id="user-1",
        thread_id="thread-1",
        active_profile=profile,
        state=state,
        step_no=0,
        plan_id=None,
        plan_hash=None,
        pending_approval_id=None,
        focus=RunFocus(in_scope_job_ids=list(focus or []), resolved_entities={}, last_citation=None),
        model_calls=0,
        tool_calls=0,
        status=RunStatus.RUNNING,
        version=0,
    )


def _turn(key: str = "turn-key-1") -> AgentTurnRecord:
    now = datetime.now(timezone.utc)
    return AgentTurnRecord(
        turn_id=f"turn-{key}",
        thread_id="thread-1",
        run_id="run-1",
        user_id="user-1",
        idempotency_key=key,
        request_hash=f"sha256:{key}",
        status="running",
        attempt=1,
        lease_owner="worker-1",
        lease_expires_at=now + timedelta(minutes=1),
        error_code=None,
        created_at=now,
        updated_at=now,
        started_at=now,
        completed_at=None,
    )


def _source_job(now: datetime) -> JobRecord:
    return JobRecord(
        id="source-1",
        project_name="source",
        analysis_type=AnalysisType.DIFFERENTIAL,
        status=JobStatus.SUCCEEDED,
        created_at=now,
        updated_at=now,
        owner_id="user-1",
    )


def _analysis_decision() -> AgentDecision:
    return AgentDecision(
        action=AgentAction.PROPOSE_PLAN,
        reasoning_summary="输入可进行差异分析",
        feasibility=Feasibility(
            verdict=FeasibilityVerdict.ANSWERABLE,
            reasons=["counts and metadata are present"],
            missing_information=[],
        ),
        analysis_recommendations=[AnalysisType.DIFFERENTIAL],
        requires_approval=True,
        requested_params={
            "compare_field": "treatment",
            "tested_levels": "salt",
            "reference_level": "control",
            "same_fields": "batch",
            "min_replicates": 2,
        },
        grounded_answer=None,
    )


def _answer_decision(*, params=None, answer=None) -> AgentDecision:
    return AgentDecision(
        action=AgentAction.ANSWER,
        reasoning_summary="仅使用本轮证据",
        feasibility=None,
        analysis_recommendations=[],
        requires_approval=False,
        requested_params=params or {},
        grounded_answer=answer,
    )


def test_analysis_without_uploaded_inputs_requests_data_without_side_effects() -> None:
    state_store = InMemoryStateStore()
    state_store.save(_state(), expected_version=0)
    plans = InMemoryPlanStore()
    approvals = InMemoryApprovalGate()
    jobs = _Jobs()
    executor = _Executor()
    model = _RecordingModel([])
    coordinator = ProductionRunCoordinator(
        state_store=state_store,
        plan_store=plans,
        approval_gate=approvals,
        event_store=InMemoryAgentEventStore(),
        model=model,
        tool_runtime=AgentToolRuntime(
            user_id="user-1",
            inputs={},
            plans=plans,
            job_store=jobs,
            files=_Files(),
            executor=executor,
            approval_gate=approvals,
        ),
    )

    result = coordinator.execute_turn(
        turn=_turn(),
        user_message="I have counts and metadata and want differential analysis",
    )

    assert result.state.state is AgentState.NEED_USER_INPUT
    assert result.state.plan_id is None
    assert result.state.pending_approval_id is None
    assert [block.type for block in result.blocks] == ["text"]
    assert "CSV" in result.blocks[0].text
    assert model.contexts == []
    assert jobs.saved == []
    assert executor.enqueued == []


def test_production_analysis_requires_structured_approval_then_submits_once() -> None:
    now = datetime.now(timezone.utc)
    state_store = InMemoryStateStore()
    state_store.save(_state(), expected_version=0)
    plans = InMemoryPlanStore()
    approvals = InMemoryApprovalGate()
    jobs = _Jobs(_source_job(now))
    files = _Files()
    executor = _Executor()
    runtime = AgentToolRuntime(
        user_id="user-1",
        inputs={
            "counts": AgentInputFile("counts.csv", b"gene,s1,s2,s3,s4\ng1,10,12,30,32\n"),
            "metadata": AgentInputFile(
                "metadata.csv",
                b"sample_id,treatment,batch\ns1,control,b1\ns2,control,b1\ns3,salt,b1\ns4,salt,b1\n",
            ),
        },
        input_source_job_id="source-1",
        plans=plans,
        job_store=jobs,
        files=files,
        executor=executor,
        approval_gate=approvals,
    )
    model = _RecordingModel([_analysis_decision()])
    coordinator = ProductionRunCoordinator(
        state_store=state_store,
        plan_store=plans,
        approval_gate=approvals,
        event_store=InMemoryAgentEventStore(),
        model=model,
        tool_runtime=runtime,
    )

    proposed = coordinator.execute_turn(turn=_turn(), user_message="比较 salt 和 control")

    assert proposed.state.status is RunStatus.SUSPENDED
    assert proposed.state.pending_approval_id
    assert {block.type for block in proposed.blocks} == {"recommendation", "plan", "approval"}
    assert jobs.saved == []
    assert executor.enqueued == []
    assert "database_url" not in model.contexts[0].model_dump_json()
    assert "counts.csv" not in model.contexts[0].model_dump_json()

    approvals.resume(
        approval_id=proposed.state.pending_approval_id,
        run_id="run-1",
        user_id="user-1",
        plan_hash=proposed.state.plan_hash or "",
        now=now,
    )
    submitted = coordinator.execute_turn(turn=_turn("approved-key"), user_message="")

    assert [block.type for block in submitted.blocks] == ["job"]
    assert len(jobs.saved) == 1
    assert len(executor.enqueued) == 1

    replay = coordinator.execute_turn(turn=_turn("approved-key"), user_message="")
    assert len(jobs.saved) == 1
    assert len(executor.enqueued) == 1
    assert replay.state.focus.in_scope_job_ids == submitted.state.focus.in_scope_job_ids


def test_model_budget_is_enforced_before_calling_the_model() -> None:
    now = datetime.now(timezone.utc)
    state_store = InMemoryStateStore()
    state_store.save(_state(), expected_version=0)
    jobs = _Jobs(_source_job(now))
    model = _RecordingModel([_analysis_decision()])
    coordinator = ProductionRunCoordinator(
        state_store=state_store,
        plan_store=InMemoryPlanStore(),
        approval_gate=InMemoryApprovalGate(),
        event_store=InMemoryAgentEventStore(),
        model=model,
        tool_runtime=AgentToolRuntime(
            user_id="user-1",
            inputs={
                "counts": AgentInputFile("counts.csv", b"gene,s1\ng1,10\n"),
                "metadata": AgentInputFile("metadata.csv", b"sample_id,treatment\ns1,control\n"),
            },
            input_source_job_id="source-1",
            job_store=jobs,
            files=_Files(),
        ),
        max_model_calls=0,
    )

    with pytest.raises(CoordinatorBudgetExceeded, match="model call budget"):
        coordinator.execute_turn(turn=_turn(), user_message="比较 salt 和 control")

    assert model.contexts == []
    assert jobs.saved == []


def test_interpretation_requeries_cited_rows_and_rejects_unsupported_number() -> None:
    now = datetime.now(timezone.utc)
    artifact = "T02_High_Confidence_Network.csv"
    job = JobRecord(
        id="job-1",
        project_name="gma",
        analysis_type=AnalysisType.CORRELATION,
        status=JobStatus.SUCCEEDED,
        created_at=now,
        updated_at=now,
        owner_id="user-1",
        artifacts=[FileArtifactInfo(
            kind=FileArtifactKind.OUTPUT,
            filename=artifact,
            path=artifact,
            storage_key="secret/storage/key",
            checksum="sha256:evidence",
            size_bytes=100,
            created_at=now,
        )],
    )
    state_store = InMemoryStateStore()
    state_store.save(_state(
        profile=ActiveProfile.INTERPRETATION,
        state=AgentState.ANSWER_WITH_EVIDENCE,
        focus=["job-1"],
    ), expected_version=0)
    draft = GroundedAnswer(claims=[GroundedClaim(
        text="GeneA 的 PearsonR 为 0.99",
        citation=Citation(artifact=artifact, checksum="sha256:evidence", row_ids=[1]),
    )])
    model = _RecordingModel([
        _answer_decision(params={"job_id": "job-1", "artifact": artifact}),
        _answer_decision(answer=draft),
    ])
    coordinator = ProductionRunCoordinator(
        state_store=state_store,
        plan_store=InMemoryPlanStore(),
        approval_gate=InMemoryApprovalGate(),
        event_store=InMemoryAgentEventStore(),
        model=model,
        tool_runtime=AgentToolRuntime(
            user_id="user-1",
            inputs={},
            job_store=_Jobs(job),
            files=_Files("Source,Target,EdgeWeight,PearsonR\nGeneA,M0123,0.82,0.71\n"),
        ),
    )

    result = coordinator.execute_turn(turn=_turn(), user_message="解释 GeneA")

    evidence = next(block for block in result.blocks if block.type == "evidence")
    assert evidence.claims[0].text.startswith("验证未通过")
    assert evidence.claims[1].text.endswith("PearsonR=0.71")
    assert model.contexts[1].evidence is not None
    assert "secret/storage/key" not in model.contexts[1].model_dump_json()
    assert "submit_approved_plan" not in model.contexts[1].model_dump_json()
    assert result.state.model_calls == 2


def test_empty_evidence_uses_fixed_template_without_second_model_call() -> None:
    now = datetime.now(timezone.utc)
    artifact = "T02_High_Confidence_Network.csv"
    job = JobRecord(
        id="job-1",
        project_name="gma",
        analysis_type=AnalysisType.CORRELATION,
        status=JobStatus.SUCCEEDED,
        created_at=now,
        updated_at=now,
        owner_id="user-1",
        artifacts=[FileArtifactInfo(
            kind=FileArtifactKind.OUTPUT,
            filename=artifact,
            path=artifact,
            storage_key="secret/storage/key",
            checksum="sha256:evidence",
            size_bytes=100,
            created_at=now,
        )],
    )
    state_store = InMemoryStateStore()
    state_store.save(_state(
        profile=ActiveProfile.INTERPRETATION,
        state=AgentState.ANSWER_WITH_EVIDENCE,
        focus=["job-1"],
    ), expected_version=0)
    model = _RecordingModel([
        _answer_decision(params={"job_id": "job-1", "artifact": artifact, "resolve_entity": "Missing"}),
    ])
    coordinator = ProductionRunCoordinator(
        state_store=state_store,
        plan_store=InMemoryPlanStore(),
        approval_gate=InMemoryApprovalGate(),
        event_store=InMemoryAgentEventStore(),
        model=model,
        tool_runtime=AgentToolRuntime(
            user_id="user-1",
            inputs={},
            job_store=_Jobs(job),
            files=_Files("Source,Target,EdgeWeight,PearsonR\nGeneA,M0123,0.82,0.71\n"),
        ),
    )

    result = coordinator.execute_turn(turn=_turn(), user_message="解释 Missing")

    evidence = next(block for block in result.blocks if block.type == "evidence")
    assert [claim.text for claim in evidence.claims] == ["没有满足阈值的证据"]
    assert result.state.model_calls == 1
    assert model.decisions == []


def test_state_conflict_replay_does_not_duplicate_approved_job_side_effect() -> None:
    now = datetime.now(timezone.utc)
    base_store = InMemoryStateStore()
    initial = _state(state=AgentState.SUBMIT_JOBS).model_copy(update={
        "plan_id": "plan-1",
        "plan_hash": "pending",
        "pending_approval_id": "pending",
    })
    plans = InMemoryPlanStore()
    plan = PlanRecord(
        plan_id="plan-1",
        run_id="run-1",
        thread_id="thread-1",
        user_id="user-1",
        analysis_type=AnalysisType.DIFFERENTIAL,
        input_source={"kind": "existing_job", "source_id": "source-1"},
        requested_params={"compare_field": "treatment"},
        effective_params={
            "compare_field": "treatment",
            "tested_levels": "salt",
            "reference_level": "control",
            "same_fields": "batch",
            "min_replicates": 2,
        },
        contrasts=[{
            "compare_field": "treatment",
            "tested_level": "salt",
            "reference_level": "control",
            "same_fields": ["batch"],
            "same_values": {"batch": "b1"},
            "tested_count": 2,
            "reference_count": 2,
        }],
        plan_hash="pending",
        approval_id=None,
    )
    plan.plan_hash = compute_plan_hash(plan)
    approvals = InMemoryApprovalGate()
    approval_id = approvals.suspend(
        run_id="run-1",
        user_id="user-1",
        plan_hash=plan.plan_hash,
        expires_at=now + timedelta(minutes=5),
    )
    approvals.resume(
        approval_id=approval_id,
        run_id="run-1",
        user_id="user-1",
        plan_hash=plan.plan_hash,
        now=now,
    )
    plan.approval_id = approval_id
    plans.save(plan)
    initial.plan_hash = plan.plan_hash
    initial.pending_approval_id = approval_id
    base_store.save(initial, expected_version=0)

    class _ConflictingStore:
        def get(self, *, run_id: str, user_id: str):
            return base_store.get(run_id=run_id, user_id=user_id)

        def save(self, state, *, expected_version: int) -> None:
            raise StateConflict(f"expected version {expected_version}")

    jobs = _Jobs(_source_job(now))
    executor = _Executor()
    coordinator = ProductionRunCoordinator(
        state_store=_ConflictingStore(),
        plan_store=plans,
        approval_gate=approvals,
        event_store=InMemoryAgentEventStore(),
        model=_RecordingModel([]),
        tool_runtime=AgentToolRuntime(
            user_id="user-1",
            inputs={
                "counts": AgentInputFile("counts.csv", b"gene,s1,s2,s3,s4\ng1,10,12,30,32\n"),
                "metadata": AgentInputFile(
                    "metadata.csv",
                    b"sample_id,treatment,batch\ns1,control,b1\ns2,control,b1\ns3,salt,b1\ns4,salt,b1\n",
                ),
            },
            input_source_job_id="source-1",
            plans=plans,
            job_store=jobs,
            files=_Files(),
            executor=executor,
            approval_gate=approvals,
        ),
    )

    for _ in range(2):
        with pytest.raises(StateConflict):
            coordinator.execute_turn(turn=_turn("same-approved-turn"), user_message="")

    assert len(jobs.saved) == 1
    assert len(executor.enqueued) == 1
