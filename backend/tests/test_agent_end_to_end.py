"""端到端多轮场景：直接驱动 ProductionRunCoordinator.execute_turn 跨越多个 turn。

与组件级 golden eval 不同，这里覆盖真实的完整状态机：
计划待批期对话、多轮对比参数协商、任务失败诊断、任务状态轮询与
evidence 语义降级。这些行为此前完全没有测试覆盖（问题 6）。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from backend.app.agent.approvals import InMemoryApprovalGate
from backend.app.agent.audit import InMemoryAgentEventStore
from backend.app.agent.model import ModelAdapter
from backend.app.agent.plans import InMemoryPlanStore
from backend.app.agent.runtime import (
    ProductionRunCoordinator,
    _job_failure_diagnosis,
    _preference_params,
)
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
    RunFocus,
    RunState,
    RunStatus,
)
from backend.app.agent.store import InMemoryStateStore, StateNotFound
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


COUNTS = AgentInputFile("counts.csv", b"gene,s1,s2,s3,s4\ng1,10,12,30,32\n")
METADATA_TREATMENT = AgentInputFile(
    "metadata.csv",
    b"sample_id,treatment\ns1,control\ns2,control\ns3,salt\ns4,salt\n",
)
METADATA_AMBIGUOUS = AgentInputFile(
    "metadata.csv",
    b"sample_id,condition\ns1,salt_high\ns2,salt_high\ns3,salt_low\ns4,salt_low\n",
)


def _state(*, profile=ActiveProfile.ANALYSIS, state=AgentState.COLLECT_INTENT, focus=None) -> RunState:
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


def _turn(key: str = "turn-1") -> AgentTurnRecord:
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


def _analysis_decision(*, params: dict | None = None) -> AgentDecision:
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
        requested_params=params if params is not None else {
            "compare_field": "treatment",
            "tested_levels": "salt",
            "reference_level": "control",
            "min_replicates": 2,
        },
        grounded_answer=None,
    )


def _answer_decision(*, params: dict | None = None, answer: GroundedAnswer | None = None) -> AgentDecision:
    return AgentDecision(
        action=AgentAction.ANSWER,
        reasoning_summary="仅使用本轮证据",
        feasibility=None,
        analysis_recommendations=[],
        requires_approval=False,
        requested_params=params or {},
        grounded_answer=answer,
    )


def _make_coordinator(
    *,
    model: ModelAdapter,
    inputs: dict,
    jobs: _Jobs | None = None,
    files: _Files | None = None,
    executor: _Executor | None = None,
    plans: InMemoryPlanStore | None = None,
    approvals: InMemoryApprovalGate | None = None,
    state_store: InMemoryStateStore | None = None,
) -> tuple[ProductionRunCoordinator, InMemoryStateStore, InMemoryPlanStore, InMemoryApprovalGate, _Jobs, _Executor]:
    state_store = state_store or InMemoryStateStore()
    try:
        state_store.get(run_id="run-1", user_id="user-1")
    except StateNotFound:
        state_store.save(_state(), expected_version=0)
    plans = plans or InMemoryPlanStore()
    approvals = approvals or InMemoryApprovalGate()
    jobs = jobs or _Jobs(_source_job(datetime.now(timezone.utc)))
    files = files or _Files()
    executor = executor or _Executor()
    runtime = AgentToolRuntime(
        user_id="user-1",
        inputs=inputs,
        input_source_job_id="source-1",
        plans=plans,
        job_store=jobs,
        files=files,
        executor=executor,
        approval_gate=approvals,
    )
    coordinator = ProductionRunCoordinator(
        state_store=state_store,
        plan_store=plans,
        approval_gate=approvals,
        event_store=InMemoryAgentEventStore(),
        model=model,
        tool_runtime=runtime,
    )
    return coordinator, state_store, plans, approvals, jobs, executor


def test_e2e_full_analysis_lifecycle_across_turns() -> None:
    """完整链路：分析→计划→批准→提交→状态轮询→完成，全程跨多个 turn。"""
    now = datetime.now(timezone.utc)
    model = _RecordingModel([_analysis_decision()])
    coordinator, _store, _plans, approvals, jobs, executor = _make_coordinator(
        model=model,
        inputs={"counts": COUNTS, "metadata": METADATA_TREATMENT},
    )

    proposed = coordinator.execute_turn(turn=_turn("propose"), user_message="比较 salt 和 control")
    assert proposed.state.state is AgentState.WAIT_EXECUTION_CONFIRMATION
    assert proposed.state.status is RunStatus.SUSPENDED
    assert {block.type for block in proposed.blocks} == {"recommendation", "plan", "approval"}
    assert jobs.saved == [] and executor.enqueued == []

    approvals.resume(
        approval_id=proposed.state.pending_approval_id,
        run_id="run-1",
        user_id="user-1",
        plan_hash=proposed.state.plan_hash or "",
        now=now,
    )
    submitted = coordinator.execute_turn(turn=_turn("approve"), user_message="")
    assert submitted.state.state is AgentState.MONITOR_JOBS
    assert [block.type for block in submitted.blocks] == ["job"]
    assert len(jobs.saved) == 1 and len(executor.enqueued) == 1

    polled = coordinator.execute_turn(turn=_turn("status"), user_message="任务跑完了吗")
    assert polled.state.state is AgentState.MONITOR_JOBS  # 未终态，继续轮询
    assert any(block.type == "job" for block in polled.blocks)

    job_id = submitted.state.focus.in_scope_job_ids[0]
    jobs.jobs[job_id].status = JobStatus.SUCCEEDED
    jobs.jobs[job_id].progress = 100
    done = coordinator.execute_turn(turn=_turn("status-done"), user_message="结果出来了吗")
    assert done.state.state is AgentState.AWAIT_FOLLOWUP
    assert any(block.type == "job" for block in done.blocks)


def test_e2e_pending_plan_answers_questions_and_stays_pending() -> None:
    """问题 1：计划待批时提问不再硬失败，且保持待批状态以便后续审批。"""
    model = _RecordingModel([_analysis_decision()])
    coordinator, _store, plans, approvals, _jobs, _executor = _make_coordinator(
        model=model,
        inputs={"counts": COUNTS, "metadata": METADATA_TREATMENT},
    )

    proposed = coordinator.execute_turn(turn=_turn("propose"), user_message="比较 salt 和 control")
    assert proposed.state.state is AgentState.WAIT_EXECUTION_CONFIRMATION

    explained = coordinator.execute_turn(
        turn=_turn("explain"),
        user_message="这个计划是什么意思",
    )
    assert [block.type for block in explained.blocks] == ["advisory"]
    assert "treatment" in explained.blocks[0].text
    assert "salt" in explained.blocks[0].text
    assert explained.state.state is AgentState.WAIT_EXECUTION_CONFIRMATION
    assert explained.state.model_calls == 1  # 计划协商用了一次，问答本身不再调模型

    generic = coordinator.execute_turn(turn=_turn("generic"), user_message="帮我看看我的数据")
    assert [block.type for block in generic.blocks] == ["text"]
    assert "待审批" in generic.blocks[0].text
    assert generic.state.state is AgentState.WAIT_EXECUTION_CONFIRMATION

    # 审批生效后，下一 turn 仍能正常提交（锚点未被普通消息消费）。
    approvals.resume(
        approval_id=proposed.state.pending_approval_id,
        run_id="run-1",
        user_id="user-1",
        plan_hash=proposed.state.plan_hash or "",
    )
    submitted = coordinator.execute_turn(turn=_turn("approve"), user_message="")
    assert submitted.state.state is AgentState.MONITOR_JOBS
    assert len(plans.get(plan_id=proposed.state.plan_id or "", user_id="user-1").submitted_job_ids) == 1


def test_e2e_contrast_negotiation_remembers_partial_params() -> None:
    """问题 2：对比参数歧义时记住已确定部分，下一轮从 confirmed_params 续填。"""
    model = _RecordingModel([
        _analysis_decision(params={"compare_field": "condition"}),
        _analysis_decision(params={"tested_levels": "salt_high", "reference_level": "salt_low"}),
    ])
    coordinator, _store, _plans, _approvals, _jobs, _executor = _make_coordinator(
        model=model,
        inputs={"counts": COUNTS, "metadata": METADATA_AMBIGUOUS},
    )

    first = coordinator.execute_turn(turn=_turn("ask"), user_message="分析一下")
    assert first.state.state is AgentState.NEED_USER_INPUT
    assert [block.type for block in first.blocks] == ["recommendation", "text"]
    assert "condition" in first.blocks[-1].text
    assert first.state.plan_id is None
    assert first.state.focus.draft_params == {"compare_field": "condition"}

    second = coordinator.execute_turn(
        turn=_turn("answer"),
        user_message="对照组=salt_low",
    )
    assert second.state.state is AgentState.WAIT_EXECUTION_CONFIRMATION
    assert {block.type for block in second.blocks} == {"recommendation", "plan", "approval"}
    assert second.state.focus.draft_params == {}
    plan = _plans.get(plan_id=second.state.plan_id or "", user_id="user-1")
    assert plan.effective_params["compare_field"] == "condition"
    assert plan.effective_params["tested_levels"] == "salt_high"
    assert plan.effective_params["reference_level"] == "salt_low"
    # 第二轮模型必须看到第一轮已确认的 compare_field。
    assert model.contexts[1].confirmed_params == {"compare_field": "condition"}


def test_e2e_job_failure_emits_diagnosis_and_sets_job_failed() -> None:
    """问题 3：任务失败时给出有依据的中文诊断，状态进入 JOB_FAILED。"""
    now = datetime.now(timezone.utc)
    failed_job = JobRecord(
        id="job-1",
        project_name="deg",
        analysis_type=AnalysisType.DIFFERENTIAL,
        status=JobStatus.FAILED,
        created_at=now,
        updated_at=now,
        owner_id="user-1",
        error="worker killed: out of memory",
    )
    state_store = InMemoryStateStore()
    state_store.save(_state(state=AgentState.MONITOR_JOBS, focus=["job-1"]), expected_version=0)
    coordinator, _store, _plans, _approvals, jobs, _executor = _make_coordinator(
        model=_RecordingModel([]),
        inputs={},
        jobs=_Jobs(failed_job),
        files=_Files(),
        state_store=state_store,
    )

    result = coordinator.execute_turn(turn=_turn("poll"), user_message="任务跑完了吗")

    assert result.state.state is AgentState.JOB_FAILED
    error_blocks = [block for block in result.blocks if block.type == "error"]
    assert len(error_blocks) == 1
    assert error_blocks[0].code == "job_failed"
    assert "out of memory" in error_blocks[0].user_message
    assert "降低输入规模" in error_blocks[0].user_message


def _evidence_job(now: datetime) -> tuple[str, JobRecord]:
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
    return artifact, job


def test_e2e_bad_artifact_query_retries_with_hint_then_succeeds() -> None:
    """任务 E：第一次选错 artifact 时在预算内自动重试，第二次选对后给出证据。"""
    now = datetime.now(timezone.utc)
    artifact, job = _evidence_job(now)
    draft = GroundedAnswer(claims=[GroundedClaim(
        text="GeneA 的 PearsonR 为 0.71",
        citation=Citation(artifact=artifact, checksum="sha256:evidence", row_ids=[1]),
    )])
    state_store = InMemoryStateStore()
    state_store.save(_state(
        profile=ActiveProfile.INTERPRETATION,
        state=AgentState.ANSWER_WITH_EVIDENCE,
        focus=["job-1"],
    ), expected_version=0)
    model = _RecordingModel([
        _answer_decision(params={"job_id": "job-999", "artifact": "wrong.csv"}),
        _answer_decision(params={"job_id": "job-1", "artifact": artifact}),
        _answer_decision(answer=draft),
    ])
    coordinator, _store, _plans, _approvals, _jobs, _executor = _make_coordinator(
        model=model,
        inputs={},
        jobs=_Jobs(job),
        files=_Files("Source,Target,EdgeWeight,PearsonR\nGeneA,M0123,0.82,0.71\n"),
        state_store=state_store,
    )

    result = coordinator.execute_turn(turn=_turn("retry-ok"), user_message="解释这个结果")

    assert result.state.state is AgentState.AWAIT_FOLLOWUP
    assert [block.type for block in result.blocks] == ["evidence"]
    assert result.state.model_calls == 3  # 查询(错) + 查询(对) + 答案
    assert model.contexts[1].retry_hint
    assert artifact in model.contexts[1].retry_hint
    assert model.decisions == []


def test_e2e_bad_artifact_query_both_attempts_fail_then_degrades() -> None:
    """任务 E：连续两次都选错 → 引导文本，状态 AWAIT_FOLLOWUP，不再有第三次调用。"""
    now = datetime.now(timezone.utc)
    artifact, job = _evidence_job(now)
    state_store = InMemoryStateStore()
    state_store.save(_state(
        profile=ActiveProfile.INTERPRETATION,
        state=AgentState.ANSWER_WITH_EVIDENCE,
        focus=["job-1"],
    ), expected_version=0)
    model = _RecordingModel([
        _answer_decision(params={"job_id": "job-999", "artifact": "wrong.csv"}),
        _answer_decision(params={"job_id": "job-998", "artifact": "also-wrong.csv"}),
    ])
    coordinator, _store, _plans, _approvals, _jobs, _executor = _make_coordinator(
        model=model,
        inputs={},
        jobs=_Jobs(job),
        files=_Files("Source,Target,EdgeWeight,PearsonR\nGeneA,M0123,0.82,0.71\n"),
        state_store=state_store,
    )

    result = coordinator.execute_turn(turn=_turn("retry-fail"), user_message="解释这个结果")

    assert result.state.state is AgentState.AWAIT_FOLLOWUP
    assert [block.type for block in result.blocks] == ["text"]
    assert artifact in result.blocks[0].text
    assert result.state.model_calls == 2
    assert model.decisions == []


def test_e2e_status_poll_refetches_jobs_while_running() -> None:
    """状态查询会在 MONITOR_JOBS 下重新拉取，而不是被当成普通咨询。"""
    now = datetime.now(timezone.utc)
    running_job = JobRecord(
        id="job-1",
        project_name="deg",
        analysis_type=AnalysisType.DIFFERENTIAL,
        status=JobStatus.RUNNING,
        progress=40,
        created_at=now,
        updated_at=now,
        owner_id="user-1",
    )
    state_store = InMemoryStateStore()
    state_store.save(_state(state=AgentState.MONITOR_JOBS, focus=["job-1"]), expected_version=0)
    coordinator, _store, _plans, _approvals, jobs, _executor = _make_coordinator(
        model=_RecordingModel([]),
        inputs={},
        jobs=_Jobs(running_job),
        files=_Files(),
        state_store=state_store,
    )

    polled = coordinator.execute_turn(turn=_turn("poll"), user_message="进度如何")
    assert polled.state.state is AgentState.MONITOR_JOBS
    job_blocks = [block for block in polled.blocks if block.type == "job"]
    assert job_blocks and job_blocks[0].status == "running"

    jobs.jobs["job-1"].status = JobStatus.SUCCEEDED
    done = coordinator.execute_turn(turn=_turn("poll-done"), user_message="任务跑完了吗")
    assert done.state.state is AgentState.AWAIT_FOLLOWUP


def test_e2e_status_query_works_after_success_in_await_followup() -> None:
    """任务 A：成功后处于 AWAIT_FOLLOWUP 也能查到任务，而不是「没有任务」。"""
    now = datetime.now(timezone.utc)
    succeeded_job = JobRecord(
        id="job-1",
        project_name="deg",
        analysis_type=AnalysisType.DIFFERENTIAL,
        status=JobStatus.SUCCEEDED,
        progress=100,
        created_at=now,
        updated_at=now,
        owner_id="user-1",
    )
    state_store = InMemoryStateStore()
    state_store.save(_state(state=AgentState.AWAIT_FOLLOWUP, focus=["job-1"]), expected_version=0)
    coordinator, _store, _plans, _approvals, _jobs, _executor = _make_coordinator(
        model=_RecordingModel([]),
        inputs={},
        jobs=_Jobs(succeeded_job),
        files=_Files(),
        state_store=state_store,
    )

    result = coordinator.execute_turn(turn=_turn("status"), user_message="结果出来了吗")

    assert [block.type for block in result.blocks] == ["job"]
    assert result.state.state is AgentState.AWAIT_FOLLOWUP
    assert not any(block.type == "text" and "没有正在运行" in block.text for block in result.blocks)


def test_e2e_status_query_in_job_failed_returns_diagnosis() -> None:
    """任务 A：失败后处于 JOB_FAILED 也能查到任务并拿到失败诊断。"""
    now = datetime.now(timezone.utc)
    failed_job = JobRecord(
        id="job-1",
        project_name="deg",
        analysis_type=AnalysisType.DIFFERENTIAL,
        status=JobStatus.FAILED,
        created_at=now,
        updated_at=now,
        owner_id="user-1",
        error="worker killed: out of memory",
    )
    state_store = InMemoryStateStore()
    state_store.save(_state(state=AgentState.JOB_FAILED, focus=["job-1"]), expected_version=0)
    coordinator, _store, _plans, _approvals, _jobs, _executor = _make_coordinator(
        model=_RecordingModel([]),
        inputs={},
        jobs=_Jobs(failed_job),
        files=_Files(),
        state_store=state_store,
    )

    result = coordinator.execute_turn(turn=_turn("status"), user_message="跑完了吗")

    error_blocks = [block for block in result.blocks if block.type == "error"]
    assert error_blocks and error_blocks[0].code == "job_failed"
    assert result.state.state is AgentState.JOB_FAILED


def test_e2e_status_query_without_tasks_returns_guidance_without_tools() -> None:
    """任务 A：focus 为空时返回引导文案，不调用任何工具。"""
    state_store = InMemoryStateStore()
    state_store.save(_state(state=AgentState.AWAIT_FOLLOWUP), expected_version=0)
    coordinator, _store, _plans, _approvals, _jobs, _executor = _make_coordinator(
        model=_RecordingModel([]),
        inputs={},
        jobs=_Jobs(),
        files=_Files(),
        state_store=state_store,
    )

    result = coordinator.execute_turn(turn=_turn("status"), user_message="任务跑完了吗")

    assert [block.type for block in result.blocks] == ["text"]
    assert "本会话还没有创建过分析任务" in result.blocks[0].text
    assert result.state.tool_calls == 0


def test_preference_params_excludes_compare_field() -> None:
    """任务 C：比较列是数据集相关的，不进入长期偏好。"""
    assert _preference_params({"compare_field": "x", "padj_cutoff": 0.05, "log2fc_cutoff": 1.0}) == {
        "padj_cutoff": 0.05,
        "log2fc_cutoff": 1.0,
    }


def test_job_failure_diagnosis_sanitizes_internal_identifiers() -> None:
    """任务 D：失败诊断不回显内部路径/校验和/长十六进制，仍保留 CSV 建议。"""
    rows = [{
        "job_id": "job-1",
        "status": "failed",
        "error": (
            "Traceback: /srv/omicsprism/storage/9f3ab1c2d4e5f60718293a4b5c6d7e8f/counts.csv "
            "not found, key=sha256:abcd1234abcd1234abcd1234abcd1234abcd1234"
        ),
    }]
    message = _job_failure_diagnosis(rows)
    assert "/srv/" not in message
    assert "sha256:" not in message
    assert not re.search(r"\b[0-9a-f]{32}\b", message)
    assert "检查 CSV 格式" in message


def test_e2e_draft_scoped_to_analysis_type() -> None:
    """任务 C：第一轮 DEG 协商留下 draft，第二轮换 GMA 后 compare_field 不再进模型。"""
    model = _RecordingModel([
        _analysis_decision(params={"compare_field": "condition"}),
        AgentDecision(
            action=AgentAction.PROPOSE_PLAN,
            reasoning_summary="输入可进行关联分析",
            feasibility=Feasibility(
                verdict=FeasibilityVerdict.ANSWERABLE,
                reasons=["three omics inputs present"],
                missing_information=[],
            ),
            analysis_recommendations=[AnalysisType.CORRELATION],
            requires_approval=True,
            requested_params={},
            grounded_answer=None,
        ),
    ])
    inputs = {
        "counts": COUNTS,
        "metadata": METADATA_AMBIGUOUS,
        "transcriptome": AgentInputFile("DEAT.csv", b"gene,s1,s2\ng1,1,2\n"),
        "metabolome": AgentInputFile("DEAM.csv", b"met,s1,s2\nm1,3,4\n"),
        "group": AgentInputFile("group.csv", b"sample_id,group\ns1,a\ns2,b\n"),
    }
    coordinator, _store, _plans, _approvals, _jobs, _executor = _make_coordinator(
        model=model,
        inputs=inputs,
    )

    first = coordinator.execute_turn(turn=_turn("deg"), user_message="分析一下")
    assert first.state.state is AgentState.NEED_USER_INPUT
    assert first.state.focus.draft_params == {"compare_field": "condition"}
    assert first.state.focus.draft_analysis_type == AnalysisType.DIFFERENTIAL.value

    coordinator.execute_turn(turn=_turn("gma"), user_message="我要做 GMA 关联分析")

    assert model.contexts[1].confirmed_params == {}


def test_e2e_source_change_clears_preferences() -> None:
    """任务 C：换输入来源后 preferences 清空，模型拿到的 confirmed_params 为空。"""
    model = _RecordingModel([_analysis_decision(), _analysis_decision()])
    coordinator, _store, _plans, _approvals, _jobs, _executor = _make_coordinator(
        model=model,
        inputs={"counts": COUNTS, "metadata": METADATA_TREATMENT},
    )

    first = coordinator.execute_turn(turn=_turn("first"), user_message="比较 salt 和 control")
    assert first.state.state is AgentState.WAIT_EXECUTION_CONFIRMATION
    assert first.state.focus.preferences  # 阈值类偏好已种子化

    # 模拟用户拒绝计划回到 NEED_USER_INPUT，再换一个输入来源。
    first.state.pending_approval_id = None
    first.state.state = AgentState.NEED_USER_INPUT
    coordinator.state_store.save(first.state, expected_version=first.state.version)
    coordinator.tool_runtime.input_source_job_id = "source-2"

    coordinator.execute_turn(turn=_turn("second"), user_message="分析一下")

    assert model.contexts[1].confirmed_params == {}
