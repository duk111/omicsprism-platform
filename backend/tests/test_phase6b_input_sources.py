from __future__ import annotations

import io
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from backend.app.agent.approvals import InMemoryApprovalGate
from backend.app.agent.plans import InMemoryPlanStore, compute_plan_hash
from backend.app.agent.product_store import AgentResourceNotFound, InMemoryAgentProductStore
from backend.app.agent.schemas import (
    AgentInputBundleRecord,
    AgentInputFileRecord,
    AgentThreadRecord,
    PlanRecord,
)
from backend.app.agent.tools import AgentToolRuntime, StagedBundleInputSource, ToolConfigurationError
from backend.app.models import AnalysisType, FileArtifactKind, UploadedFileInfo


class _StagedFiles:
    def __init__(self) -> None:
        self.payloads = {
            "agent-inputs/bundle-1/counts.csv": b"gene,s1,s2,s3,s4\ng1,10,12,30,32\n",
            "agent-inputs/bundle-1/metadata.csv": (
                b"sample_id,treatment,batch\n"
                b"s1,control,b1\n"
                b"s2,control,b1\n"
                b"s3,salt,b1\n"
                b"s4,salt,b1\n"
            ),
        }
        self.copies: list[tuple[str, str]] = []

    def open_storage_key(self, storage_key: str):
        return io.BytesIO(self.payloads[storage_key])

    def copy_staged_input(self, target_job_id: str, item: AgentInputFileRecord) -> UploadedFileInfo:
        self.copies.append((target_job_id, item.file_id))
        return UploadedFileInfo(
            kind=FileArtifactKind.INPUT,
            field=item.field,
            filename=item.filename,
            path=f"inputs/{item.filename}",
            storage_key=f"jobs/{target_job_id}/inputs/{item.filename}",
            checksum=item.checksum,
            content_type=item.content_type,
            size_bytes=item.size_bytes,
            created_at=item.created_at,
        )


def _store(now: datetime) -> InMemoryAgentProductStore:
    counts = b"gene,s1,s2,s3,s4\ng1,10,12,30,32\n"
    metadata = (
        b"sample_id,treatment,batch\n"
        b"s1,control,b1\n"
        b"s2,control,b1\n"
        b"s3,salt,b1\n"
        b"s4,salt,b1\n"
    )
    store = InMemoryAgentProductStore()
    store.save_thread(AgentThreadRecord(
        thread_id="thread-1",
        user_id="user-1",
        title="bundle",
        current_run_id="run-1",
        status="active",
        version=0,
        created_at=now,
        updated_at=now,
    ))
    store.save_input_bundle(AgentInputBundleRecord(
        bundle_id="bundle-1",
        thread_id="thread-1",
        user_id="user-1",
        status="active",
        expires_at=now + timedelta(hours=1),
        created_at=now,
    ))
    store.append_input_file(AgentInputFileRecord(
        file_id="file-1",
        bundle_id="bundle-1",
        user_id="user-1",
        field="counts",
        filename="counts.csv",
        storage_key="agent-inputs/bundle-1/counts.csv",
        checksum="sha256:" + hashlib.sha256(counts).hexdigest(),
        content_type="text/csv",
        size_bytes=len(counts),
        created_at=now,
    ))
    store.append_input_file(AgentInputFileRecord(
        file_id="file-2",
        bundle_id="bundle-1",
        user_id="user-1",
        field="metadata",
        filename="metadata.csv",
        storage_key="agent-inputs/bundle-1/metadata.csv",
        checksum="sha256:" + hashlib.sha256(metadata).hexdigest(),
        content_type="text/csv",
        size_bytes=len(metadata),
        created_at=now,
    ))
    return store


def test_staged_bundle_source_is_ownership_bound_and_copies_only_on_request() -> None:
    now = datetime.now(timezone.utc)
    store = _store(now)
    files = _StagedFiles()

    source = StagedBundleInputSource(
        user_id="user-1",
        thread_id="thread-1",
        bundle_id="bundle-1",
        product_store=store,
        files=files,
        now=now,
    )

    assert source.ref.model_dump(mode="json") == {
        "kind": "staged_bundle",
        "source_id": "bundle-1",
    }
    assert source.load_inputs()["counts"].content == b"gene,s1,s2,s3,s4\ng1,10,12,30,32\n"
    assert files.copies == []

    copied = source.copy_inputs("job-1")
    assert [item.field for item in copied] == ["counts", "metadata"]
    assert files.copies == [("job-1", "file-1"), ("job-1", "file-2")]

    with pytest.raises(AgentResourceNotFound):
        StagedBundleInputSource(
            user_id="user-2",
            thread_id="thread-1",
            bundle_id="bundle-1",
            product_store=store,
            files=files,
            now=now,
        )

    with pytest.raises(AgentResourceNotFound):
        StagedBundleInputSource(
            user_id="user-1",
            thread_id="thread-2",
            bundle_id="bundle-1",
            product_store=store,
            files=files,
            now=now,
        )

    files.payloads["agent-inputs/bundle-1/counts.csv"] = b"gene,s1\ng1,999\n"
    with pytest.raises(ToolConfigurationError, match="checksum changed"):
        source.load_inputs()


def test_staged_bundle_creates_exactly_one_job_only_after_structured_approval() -> None:
    now = datetime.now(timezone.utc)
    store = _store(now)
    files = _StagedFiles()
    source = StagedBundleInputSource(
        user_id="user-1",
        thread_id="thread-1",
        bundle_id="bundle-1",
        product_store=store,
        files=files,
        now=now,
    )
    plans = InMemoryPlanStore()
    approvals = InMemoryApprovalGate()

    class _Jobs:
        def __init__(self) -> None:
            self.saved = []

        def get_for_user(self, job_id: str, user_id: str):
            match = next((item for item in self.saved if item.id == job_id and item.owner_id == user_id), None)
            if match is None:
                raise LookupError(job_id)
            return match

        def save(self, job) -> None:
            self.saved.append(job)

    class _Executor:
        def __init__(self) -> None:
            self.enqueued: list[str] = []

        def enqueue(self, job_id: str) -> None:
            self.enqueued.append(job_id)

    jobs = _Jobs()
    executor = _Executor()
    runtime = AgentToolRuntime.from_input_source(
        user_id="user-1",
        input_source=source,
        plans=plans,
        job_store=jobs,
        files=files,
        executor=executor,
        approval_gate=approvals,
    )
    plan = PlanRecord(
        plan_id="plan-staged-1",
        run_id="run-1",
        thread_id="thread-1",
        user_id="user-1",
        analysis_type=AnalysisType.DIFFERENTIAL,
        input_source=source.ref,
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
    plans.save(plan)

    blocked = runtime.submit_approved_plan(plan.plan_id, "staged-idempotency-key")
    assert not blocked.ok and blocked.error_code == "approval_required"
    assert jobs.saved == []
    assert executor.enqueued == []
    assert files.copies == []

    approval_id = approvals.suspend(
        plan_id=plan.plan_id,
        thread_id=plan.thread_id,
        run_id=plan.run_id,
        user_id=plan.user_id,
        plan_hash=plan.plan_hash,
        expires_at=now + timedelta(hours=1),
    )
    approvals.resume(
        approval_id=approval_id,
        run_id=plan.run_id,
        user_id=plan.user_id,
        plan_hash=plan.plan_hash,
        now=now,
    )
    plan.approval_id = approval_id
    plans.save(plan)

    submitted = runtime.submit_approved_plan(plan.plan_id, "staged-idempotency-key")
    replayed = runtime.submit_approved_plan(plan.plan_id, "staged-idempotency-key")

    assert submitted.ok and replayed.ok
    assert len(jobs.saved) == 1
    assert len(executor.enqueued) == 1
    assert len(files.copies) == 2
    assert submitted.rows[0]["job_ids"] == replayed.rows[0]["job_ids"]
