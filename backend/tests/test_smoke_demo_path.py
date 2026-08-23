from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from backend.app.agent.approvals import InMemoryApprovalGate
from backend.app.agent.context import build_input_summaries
from backend.app.agent.grounding import EvidenceGrounder
from backend.app.agent.plans import InMemoryPlanStore, compute_plan_hash
from backend.app.agent.runtime import _complete_contrast_params
from backend.app.agent.schemas import PlanRecord
from backend.app.agent.tools import AgentInputFile, AgentToolRuntime
from backend.app.agent.verifier import AnswerVerifier
from backend.app.models import AnalysisType, FileArtifactInfo, FileArtifactKind, JobRecord, JobStatus


class _FakeStructuredProposalModel:
    """Local structured-output stand-in; the smoke path never calls a network model."""

    def propose(self, _message: str, _summaries: list[object]) -> dict[str, object]:
        return {
            "analysis_type": "DEG",
            "params": {
                "compare_field": "treatment",
                "tested_levels": "salt",
                "reference_level": "control",
                "min_replicates": 2,
            },
        }


class _Jobs:
    def __init__(self, source: JobRecord) -> None:
        self.jobs = {source.id: source}
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
    result_text = "gene,log2FoldChange,padj\nGeneA,2.5,0.01\n"

    def copy_input_artifact(self, _source_job_id: str, _target_job_id: str, source: object) -> object:
        return source

    def read_artifact_text(self, _job_id: str, _relative_path: str, *, max_chars: int | None = None) -> str:
        return self.result_text[:max_chars] if max_chars else self.result_text


class _Executor:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    def enqueue(self, job_id: str) -> None:
        self.enqueued.append(job_id)


def test_smoke_demo_path_keeps_analysis_and_grounding_contracts() -> None:
    now = datetime.now(timezone.utc)
    user_id = "user-1"
    source = JobRecord(
        id="source-1",
        project_name="fixture source",
        analysis_type=AnalysisType.DIFFERENTIAL,
        status=JobStatus.SUCCEEDED,
        created_at=now,
        updated_at=now,
        owner_id=user_id,
    )
    jobs = _Jobs(source)
    files = _Files()
    executor = _Executor()
    plans = InMemoryPlanStore()
    approvals = InMemoryApprovalGate()
    runtime = AgentToolRuntime(
        user_id=user_id,
        inputs={
            "counts": AgentInputFile(
                "counts.csv",
                b"gene,s1,s2,s3,s4\ng1,10,12,30,32\ng2,8,9,20,22\n",
            ),
            "metadata": AgentInputFile(
                "metadata.csv",
                b"sample_id,treatment\ns1,control\ns2,control\ns3,salt\ns4,salt\n",
            ),
        },
        input_source_job_id=source.id,
        plans=plans,
        job_store=jobs,
        files=files,
        executor=executor,
        approval_gate=approvals,
    )

    inspected = runtime.inspect_uploaded_inputs()
    summaries = build_input_summaries(inspected.rows)
    proposal = _FakeStructuredProposalModel().propose("比较 salt 和 control", summaries)
    resolved, clarification = _complete_contrast_params(
        AnalysisType.DIFFERENTIAL,
        dict(proposal["params"]),
        summaries,
    )
    assert clarification is None
    assert resolved["compare_field"] == "treatment"
    assert resolved["tested_levels"] == "salt"
    assert resolved["reference_level"] == "control"

    validation = runtime.run_preflight(AnalysisType.DIFFERENTIAL, resolved)
    assert validation.ok
    confirmation_payload = {
        "analysis_type": proposal["analysis_type"],
        "resolved_params": validation.rows[0]["effective_params"],
        "contrasts": validation.rows[0]["contrasts"],
    }
    assert confirmation_payload["contrasts"][0]["tested_count"] == 2
    assert confirmation_payload["contrasts"][0]["reference_count"] == 2

    plan = PlanRecord(
        plan_id="plan-smoke",
        run_id="run-smoke",
        thread_id="thread-smoke",
        user_id=user_id,
        analysis_type=AnalysisType.DIFFERENTIAL,
        input_source={"kind": "existing_job", "source_id": source.id},
        requested_params=dict(proposal["params"]),
        effective_params=validation.rows[0]["effective_params"],
        contrasts=validation.rows[0]["contrasts"],
        plan_hash="pending",
        approval_id=None,
    )
    plan.plan_hash = compute_plan_hash(plan)
    plan.approval_id = approvals.suspend(
        run_id=plan.run_id,
        user_id=user_id,
        plan_hash=plan.plan_hash,
        expires_at=now + timedelta(minutes=5),
    )
    plans.save(plan)
    approvals.resume(
        approval_id=plan.approval_id,
        run_id=plan.run_id,
        user_id=user_id,
        plan_hash=plan.plan_hash,
        now=now,
    )

    submitted = runtime.submit_approved_plan(plan.plan_id, "smoke-idempotency")
    assert submitted.ok
    job_id = submitted.rows[0]["job_ids"][0]
    assert jobs.saved and executor.enqueued == [job_id]

    artifact = FileArtifactInfo(
        kind=FileArtifactKind.OUTPUT,
        filename="differential_gene_counts.csv",
        path="differential_gene_counts.csv",
        storage_key="fixture/result.csv",
        checksum="sha256:fixture-result",
        size_bytes=len(files.result_text.encode()),
        created_at=now,
    )
    jobs.jobs[job_id].artifacts = [artifact]
    evidence = runtime.query_result_evidence(job_id, artifact.filename)
    assert evidence.ok
    assert evidence.artifact == artifact.path
    assert evidence.checksum == artifact.checksum
    assert evidence.rows[0]["_row_id"] == 1

    answer = EvidenceGrounder().ground(evidence)
    assert any("2.5" in claim.text for claim in answer.claims)
    assert answer.claims[0].citation.artifact == artifact.path
    assert AnswerVerifier().verify(answer, [evidence]).verdict.value == "approved"
