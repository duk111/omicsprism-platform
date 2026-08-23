from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from backend.app.agent.tools import AgentToolRuntime
from backend.app.models import (
    AnalysisType,
    FileArtifactInfo,
    FileArtifactKind,
    JobRecord,
    JobStatus,
)


class _FakeJobs:
    def __init__(self, source: JobRecord) -> None:
        self.jobs = {source.id: source}

    def get_for_user(self, job_id: str, user_id: str) -> JobRecord:
        job = self.jobs.get(job_id)
        if job is None or job.owner_id != user_id:
            raise HTTPException(status_code=404, detail="Job not found")
        return job


class _FakeFiles:
    def __init__(self, content: str | None = None) -> None:
        self.content = content or (
            "Gene,EdgeWeight,PearsonR\n"
            "GeneA,0.82,0.71\n"
            "GeneB,0.51,0.20\n"
        )

    def recent_log(self, _job_id: str):
        return None, None

    def read_artifact_text(
        self,
        _job_id: str,
        _relative_path: str,
        *,
        max_chars: int | None = None,
    ) -> str:
        return self.content[:max_chars] if max_chars else self.content


def _runtime(job: JobRecord, content: str | None = None) -> AgentToolRuntime:
    return AgentToolRuntime(
        user_id="user-1",
        job_store=_FakeJobs(job),
        files=_FakeFiles(content),
    )


def _job(*, owner_id: str = "user-1", artifact: FileArtifactInfo | None = None) -> JobRecord:
    now = datetime.now(timezone.utc)
    return JobRecord(
        id="job-1",
        project_name="fixture",
        analysis_type=AnalysisType.CORRELATION,
        status=JobStatus.SUCCEEDED,
        created_at=now,
        updated_at=now,
        owner_id=owner_id,
        artifacts=[artifact] if artifact else [],
    )


def _artifact(*, size_bytes: int = 64) -> FileArtifactInfo:
    return FileArtifactInfo(
        kind=FileArtifactKind.OUTPUT,
        filename="T02_High_Confidence_Network.csv",
        path="T02_High_Confidence_Network.csv",
        storage_key="fixture/result.csv",
        checksum="sha256:fixture",
        size_bytes=size_bytes,
        created_at=datetime.now(timezone.utc),
    )


def test_get_job_and_query_result_are_user_bound() -> None:
    artifact = _artifact()
    runtime = _runtime(_job(artifact=artifact))

    status = runtime.get_job("job-1")
    assert status.ok and status.rows[0]["status"] == "succeeded"
    assert status.rows[0]["artifacts"] == [artifact.filename]

    evidence = runtime.query_result(
        "job-1",
        artifact.filename,
        filters={"Gene": "GeneA"},
        sort="EdgeWeight desc",
        limit=10,
    )
    assert evidence.ok
    assert evidence.rows == [{
        "_row_id": 1,
        "Gene": "GeneA",
        "EdgeWeight": "0.82",
        "PearsonR": "0.71",
    }]
    assert evidence.checksum == "sha256:fixture"


def test_result_artifact_allowlist_rejects_path_escape() -> None:
    result = _runtime(_job()).query_result("job-1", "../secret.csv")

    assert not result.ok
    assert result.error_code == "artifact_not_allowed"


def test_result_access_preserves_cross_user_404() -> None:
    runtime = _runtime(_job(owner_id="user-2", artifact=_artifact()))

    with pytest.raises(HTTPException) as exc:
        runtime.query_result("job-1", "T02_High_Confidence_Network.csv")
    assert exc.value.status_code == 404


def test_result_rows_are_capped_by_count_and_serialized_size() -> None:
    artifact = _artifact(size_bytes=100_000)
    payload = "Gene,Detail\n" + "".join(
        f"Gene{index},{'x' * 2000}\n" for index in range(100)
    )
    result = _runtime(_job(artifact=artifact), payload).query_result(
        "job-1",
        artifact.filename,
    )

    assert result.row_count == 100
    assert len(result.rows) <= 50
    assert result.truncated
    assert len(result.model_dump_json().encode("utf-8")) <= 32 * 1024
