from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from backend.app.agent.approvals import InMemoryApprovalGate, JsonApprovalGate
from backend.app.agent.plans import InMemoryPlanStore, compute_plan_hash
from backend.app.agent.plans import JsonPlanStore
from backend.app.agent.policy import ProfilePolicyGuard, ProfilePolicyViolation
from backend.app.agent.schemas import ActiveProfile, PlanRecord
from backend.app.agent.tools import AgentInputFile, AgentToolRuntime, PolicyToolExecutor, ToolConfigurationError, ToolRegistry
from backend.app.models import AnalysisType, JobRecord, JobStatus
from backend.app.models import FileArtifactInfo, FileArtifactKind


def _runtime() -> AgentToolRuntime:
    return AgentToolRuntime(
        user_id="user-1",
        inputs={
            "counts": AgentInputFile("counts.csv", b"gene,s1,s2,s3,s4\ng1,10,12,30,32\n"),
            "metadata": AgentInputFile(
                "metadata.csv",
                b"sample_id,treatment,batch\ns1,control,b1\ns2,control,b1\ns3,salt,b1\ns4,salt,b1\n",
            ),
        },
    )


def test_unconfigured_registry_fails_explicitly_without_fake_capability() -> None:
    registry = ToolRegistry()

    try:
        registry.call("get_analysis_spec", analysis_type="differential")
    except ToolConfigurationError:
        return
    raise AssertionError("unconfigured registry must not pretend to execute a tool")


def test_configured_registry_returns_spec_and_real_preflight_result() -> None:
    registry = ToolRegistry(_runtime())

    spec = registry.call("get_analysis_spec", analysis_type=AnalysisType.DIFFERENTIAL)
    assert spec.ok
    assert spec.rows[0]["display_label"] == "DEG"

    result = registry.call(
        "run_preflight",
        analysis_type=AnalysisType.DIFFERENTIAL,
        params={
            "compare_field": "treatment",
            "tested_levels": "salt",
            "reference_level": "control",
            "same_fields": "batch",
            "min_replicates": 2,
        },
    )
    assert result.ok
    assert result.rows[0]["contrasts"][0]["tested_level"] == "salt"
    assert result.rows[0]["effective_params"]["min_replicates"] == 2

    inspected = registry.call("inspect_uploaded_inputs")
    counts = next(row for row in inspected.rows if row["field"] == "counts")
    metadata = next(row for row in inspected.rows if row["field"] == "metadata")
    assert counts["dtype"] == "numeric"
    assert counts["min"] == 10.0 and counts["max"] == 32.0
    assert metadata["group_replicates"]["treatment"] == {"control": 2, "salt": 2}


def test_large_counts_preflight_keeps_result_within_tool_boundary() -> None:
    rows = "".join(f"gene_{index},10,12,30,32\n" for index in range(5000))
    runtime = AgentToolRuntime(
        user_id="user-1",
        inputs={
            "counts": AgentInputFile(
                "raw_count.csv",
                ("gene,s1,s2,s3,s4\n" + rows).encode(),
            ),
            "metadata": AgentInputFile(
                "metadata.csv",
                b"sample_id,treatment\ns1,control\ns2,control\ns3,salt\ns4,salt\n",
            ),
        },
    )

    result = runtime.run_preflight(
        AnalysisType.DIFFERENTIAL,
        {
            "compare_field": "treatment",
            "tested_levels": "salt",
            "reference_level": "control",
            "min_replicates": 2,
        },
    )

    assert result.ok
    assert not result.truncated
    assert result.rows[0]["contrasts"][0]["tested_level"] == "salt"
    counts = next(item for item in result.rows[0]["files"] if item["field"] == "counts")
    assert counts["feature_count"] == 5000
    assert "feature_ids" not in counts


class _FakeJobs:
    def __init__(self, source: JobRecord) -> None:
        self.jobs = {source.id: source}
        self.saved: list[JobRecord] = []

    def get_for_user(self, job_id: str, user_id: str) -> JobRecord:
        job = self.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.owner_id != user_id:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    def save(self, job: JobRecord) -> None:
        self.jobs[job.id] = job
        self.saved.append(job)


class _FakeFiles:
    def __init__(self, content: str | None = None) -> None:
        self.content = content or "Gene,EdgeWeight,PearsonR\nGeneA,0.82,0.71\nGeneB,0.51,0.20\n"

    def copy_input_artifact(self, source_job_id: str, target_job_id: str, source):
        return source

    def recent_log(self, job_id: str):
        return None, None

    def read_artifact_text(self, job_id: str, relative_path: str, *, max_chars: int | None = None) -> str:
        return self.content


class _FakeExecutor:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    def enqueue(self, job_id: str) -> None:
        self.enqueued.append(job_id)


def _plan() -> PlanRecord:
    plan = PlanRecord(
        plan_id="plan-1",
        run_id="run-1",
        thread_id="thread-1",
        user_id="user-1",
        analysis_type=AnalysisType.DIFFERENTIAL,
        input_source={"kind": "existing_job", "source_id": "source-1"},
        requested_params={"compare_field": "treatment", "tested_levels": "salt", "reference_level": "control"},
        effective_params={"compare_field": "treatment", "tested_levels": "salt", "reference_level": "control"},
        contrasts=[{
            "compare_field": "treatment",
            "tested_level": "salt",
            "reference_level": "control",
            "same_fields": [],
            "same_values": {},
            "tested_count": 2,
            "reference_count": 2,
        }],
        plan_hash="pending",
        approval_id="approval-1",
    )
    plan.plan_hash = compute_plan_hash(plan)
    return plan


def _submit_runtime(plan: PlanRecord, gate: InMemoryApprovalGate, jobs: _FakeJobs, executor: _FakeExecutor):
    plans = InMemoryPlanStore()
    plans.save(plan)
    return AgentToolRuntime(
        user_id="user-1",
        inputs=_runtime().inputs,
        input_source_job_id="source-1",
        plans=plans,
        job_store=jobs,
        files=_FakeFiles(),
        executor=executor,
        approval_gate=gate,
    )


def test_submit_requires_approval_and_valid_contrast_before_side_effect() -> None:
    now = datetime.now(timezone.utc)
    source = JobRecord(
        id="source-1", project_name="source", analysis_type=AnalysisType.DIFFERENTIAL,
        status=JobStatus.SUCCEEDED, created_at=now, updated_at=now, owner_id="user-1",
    )
    jobs, executor = _FakeJobs(source), _FakeExecutor()
    gate = InMemoryApprovalGate()
    plan = _plan()
    approval_id = gate.suspend(run_id="run-1", user_id="user-1", plan_hash=plan.plan_hash, expires_at=now + timedelta(minutes=5))
    plan.approval_id = approval_id
    runtime = _submit_runtime(plan, gate, jobs, executor)

    blocked = runtime.submit_approved_plan(plan.plan_id, "key-1")
    assert not blocked.ok
    assert blocked.error_code == "approval_required"
    assert jobs.saved == []
    assert executor.enqueued == []


def test_submit_is_idempotent_for_replayed_key() -> None:
    now = datetime.now(timezone.utc)
    source = JobRecord(
        id="source-1", project_name="source", analysis_type=AnalysisType.DIFFERENTIAL,
        status=JobStatus.SUCCEEDED, created_at=now, updated_at=now, owner_id="user-1",
    )
    jobs, executor = _FakeJobs(source), _FakeExecutor()
    gate = InMemoryApprovalGate()
    plan = _plan()
    approval_id = gate.suspend(run_id="run-1", user_id="user-1", plan_hash=plan.plan_hash, expires_at=now + timedelta(minutes=5))
    gate.resume(approval_id=approval_id, run_id="run-1", user_id="user-1", plan_hash=plan.plan_hash, now=now)
    plan.approval_id = approval_id
    runtime = _submit_runtime(plan, gate, jobs, executor)

    first = runtime.submit_approved_plan(plan.plan_id, "key-1")
    second = runtime.submit_approved_plan(plan.plan_id, "key-1")
    assert first.ok and second.ok
    assert first.rows[0]["job_ids"] == second.rows[0]["job_ids"]
    assert len(jobs.saved) == 1
    assert executor.enqueued == [first.rows[0]["job_ids"][0]]


@pytest.mark.parametrize(("mutation", "error_code"), [
    ("empty_contrasts", "preflight_blocked"),
    ("changed_params", "plan_hash_mismatch"),
])
def test_submit_rejects_invalidated_plan_without_creating_job(mutation: str, error_code: str) -> None:
    now = datetime.now(timezone.utc)
    source = JobRecord(
        id="source-1", project_name="source", analysis_type=AnalysisType.DIFFERENTIAL,
        status=JobStatus.SUCCEEDED, created_at=now, updated_at=now, owner_id="user-1",
    )
    jobs, executor = _FakeJobs(source), _FakeExecutor()
    gate = InMemoryApprovalGate()
    plan = _plan()
    approval_id = gate.suspend(
        run_id=plan.run_id, user_id=plan.user_id, plan_hash=plan.plan_hash,
        expires_at=now + timedelta(minutes=5),
    )
    gate.resume(
        approval_id=approval_id, run_id=plan.run_id, user_id=plan.user_id,
        plan_hash=plan.plan_hash, now=now,
    )
    plan.approval_id = approval_id
    if mutation == "empty_contrasts":
        plan.contrasts = []
        plan.plan_hash = compute_plan_hash(plan)
    else:
        plan.effective_params["tested_levels"] = "drought"
    runtime = _submit_runtime(plan, gate, jobs, executor)

    result = runtime.submit_approved_plan(plan.plan_id, "key-invalid")
    assert not result.ok and result.error_code == error_code
    assert jobs.saved == []
    assert executor.enqueued == []


def test_submit_rejects_expired_approval_without_creating_job() -> None:
    now = datetime.now(timezone.utc)
    source = JobRecord(
        id="source-1", project_name="source", analysis_type=AnalysisType.DIFFERENTIAL,
        status=JobStatus.SUCCEEDED, created_at=now, updated_at=now, owner_id="user-1",
    )
    jobs, executor = _FakeJobs(source), _FakeExecutor()
    gate = InMemoryApprovalGate()
    plan = _plan()
    plan.approval_id = gate.suspend(
        run_id=plan.run_id, user_id=plan.user_id, plan_hash=plan.plan_hash,
        expires_at=now - timedelta(seconds=1),
    )
    runtime = _submit_runtime(plan, gate, jobs, executor)

    result = runtime.submit_approved_plan(plan.plan_id, "key-expired")
    assert not result.ok and result.error_code == "approval_required"
    assert jobs.saved == []
    assert executor.enqueued == []


def test_submit_preserves_cross_user_source_job_404() -> None:
    now = datetime.now(timezone.utc)
    other_users_source = JobRecord(
        id="source-1", project_name="source", analysis_type=AnalysisType.DIFFERENTIAL,
        status=JobStatus.SUCCEEDED, created_at=now, updated_at=now, owner_id="user-2",
    )
    jobs, executor = _FakeJobs(other_users_source), _FakeExecutor()
    gate = InMemoryApprovalGate()
    plan = _plan()
    approval_id = gate.suspend(
        run_id=plan.run_id, user_id=plan.user_id, plan_hash=plan.plan_hash,
        expires_at=now + timedelta(minutes=5),
    )
    gate.resume(
        approval_id=approval_id, run_id=plan.run_id, user_id=plan.user_id,
        plan_hash=plan.plan_hash, now=now,
    )
    plan.approval_id = approval_id
    runtime = _submit_runtime(plan, gate, jobs, executor)

    with pytest.raises(HTTPException) as exc:
        runtime.submit_approved_plan(plan.plan_id, "key-cross-user")
    assert exc.value.status_code == 404
    assert jobs.saved == []
    assert executor.enqueued == []


def test_status_and_result_evidence_are_user_bound_and_field_based() -> None:
    now = datetime.now(timezone.utc)
    source = JobRecord(
        id="source-1", project_name="source", analysis_type=AnalysisType.CORRELATION,
        status=JobStatus.SUCCEEDED, created_at=now, updated_at=now, owner_id="user-1",
        artifacts=[FileArtifactInfo(
            kind=FileArtifactKind.OUTPUT, filename="T02_High_Confidence_Network.csv",
            path="outputs/T02_High_Confidence_Network.csv", storage_key="key",
            checksum="sha256:fixture", size_bytes=64, created_at=now,
        )],
    )
    jobs = _FakeJobs(source)
    runtime = AgentToolRuntime(user_id="user-1", inputs={}, job_store=jobs, files=_FakeFiles())

    status = runtime.get_jobs_status(["source-1"])
    assert status.ok and status.rows[0]["status"] == "succeeded"
    evidence = runtime.query_result_evidence(
        "source-1", "T02_High_Confidence_Network.csv",
        filters={"Gene": "GeneA"}, sort="EdgeWeight desc", limit=10,
    )
    assert evidence.ok
    assert evidence.rows == [{"_row_id": 1, "Gene": "GeneA", "EdgeWeight": "0.82", "PearsonR": "0.71"}]
    assert evidence.checksum == "sha256:fixture"


def test_result_artifact_allowlist_rejects_path_escape() -> None:
    now = datetime.now(timezone.utc)
    source = JobRecord(
        id="source-1", project_name="source", analysis_type=AnalysisType.CORRELATION,
        status=JobStatus.SUCCEEDED, created_at=now, updated_at=now, owner_id="user-1",
    )
    runtime = AgentToolRuntime(user_id="user-1", inputs={}, job_store=_FakeJobs(source), files=_FakeFiles())

    result = runtime.query_result_evidence("source-1", "../secret.csv")
    assert not result.ok
    assert result.error_code == "artifact_not_allowed"


def test_result_access_preserves_cross_user_404() -> None:
    now = datetime.now(timezone.utc)
    other_users_job = JobRecord(
        id="source-1", project_name="source", analysis_type=AnalysisType.CORRELATION,
        status=JobStatus.SUCCEEDED, created_at=now, updated_at=now, owner_id="user-2",
    )
    runtime = AgentToolRuntime(
        user_id="user-1", inputs={}, job_store=_FakeJobs(other_users_job), files=_FakeFiles(),
    )

    with pytest.raises(HTTPException) as exc:
        runtime.query_result_evidence("source-1", "T02_High_Confidence_Network.csv")
    assert exc.value.status_code == 404


def test_result_rows_are_capped_by_count_and_serialized_size() -> None:
    now = datetime.now(timezone.utc)
    artifact = FileArtifactInfo(
        kind=FileArtifactKind.OUTPUT, filename="T02_High_Confidence_Network.csv",
        path="outputs/T02_High_Confidence_Network.csv", storage_key="key",
        checksum="sha256:fixture", size_bytes=100000, created_at=now,
    )
    job = JobRecord(
        id="source-1", project_name="source", analysis_type=AnalysisType.CORRELATION,
        status=JobStatus.SUCCEEDED, created_at=now, updated_at=now, owner_id="user-1",
        artifacts=[artifact],
    )
    payload = "Gene,Detail\n" + "".join(f"Gene{i},{'x' * 2000}\n" for i in range(100))
    runtime = AgentToolRuntime(
        user_id="user-1", inputs={}, job_store=_FakeJobs(job), files=_FakeFiles(payload),
    )

    result = runtime.query_result_evidence("source-1", artifact.filename)
    assert result.row_count == 100
    assert len(result.rows) <= 50
    assert result.truncated
    assert len(result.model_dump_json().encode("utf-8")) <= 32 * 1024


def test_json_plan_store_persists_and_binds_user(tmp_path) -> None:
    plan = _plan()
    store = JsonPlanStore(tmp_path / "plans")
    store.save(plan)

    assert store.get(plan_id=plan.plan_id, user_id="user-1").plan_hash == plan.plan_hash
    with pytest.raises(LookupError):
        store.get(plan_id=plan.plan_id, user_id="user-2")


def test_policy_executor_rejects_interpretation_write_before_handler() -> None:
    runtime = _runtime()
    registry = ToolRegistry(runtime)
    executor = PolicyToolExecutor(
        registry,
        runtime=runtime,
        active_profile=ActiveProfile.INTERPRETATION,
        policy=ProfilePolicyGuard(),
    )

    with pytest.raises(ProfilePolicyViolation):
        executor.execute("submit_approved_plan", plan_id="plan-1", idempotency_key="key-1")


def test_json_approval_gate_survives_reconstruction(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    first = JsonApprovalGate(tmp_path / "approvals")
    approval_id = first.suspend(
        run_id="run-1", user_id="user-1", plan_hash="sha256:plan",
        expires_at=now + timedelta(minutes=5),
    )
    first.resume(
        approval_id=approval_id, run_id="run-1", user_id="user-1",
        plan_hash="sha256:plan", now=now,
    )

    restarted = JsonApprovalGate(tmp_path / "approvals")
    assert restarted.is_valid(
        approval_id=approval_id, run_id="run-1", user_id="user-1",
        plan_hash="sha256:plan", now=now,
    )
