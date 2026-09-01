from __future__ import annotations

from datetime import datetime, timezone

from backend.app.agent.param_resolver import ScopeSpec
from backend.app.agent.tools import AgentInputFile, AgentToolRuntime
from backend.app.models import (
    AnalysisType,
    FileArtifactInfo,
    FileArtifactKind,
    JobRecord,
    JobStatus,
)


class _Jobs:
    def __init__(self, jobs: list[JobRecord]) -> None:
        self.jobs = {job.id: job for job in jobs}

    def get_for_user(self, job_id: str, user_id: str) -> JobRecord:
        job = self.jobs.get(job_id)
        if job is None or job.owner_id != user_id:
            raise LookupError(job_id)
        return job

    def list_for_user(self, user_id: str, include_deleted: bool = False) -> list[JobRecord]:
        return [
            job for job in self.jobs.values()
            if job.owner_id == user_id and (include_deleted or job.deleted_at is None)
        ]


class _Files:
    def __init__(self, content: str) -> None:
        self.content = content

    def recent_log(self, _job_id: str):
        return None, None

    def read_artifact_text(self, _job_id: str, _relative_path: str, *, max_chars: int | None = None) -> str:
        return self.content[:max_chars] if max_chars else self.content


def _inputs() -> dict[str, AgentInputFile]:
    rows = [
        "sample_id,line,timepoint,treatment",
        "s1,WT,24h,control",
        "s2,WT,24h,control",
        "s3,WT,24h,salt",
        "s4,WT,24h,salt",
        "s5,WT,48h,control",
        "s6,WT,48h,salt",
        "s7,mutant,24h,control",
        "s8,mutant,24h,salt",
    ]
    return {
        "counts": AgentInputFile("counts.csv", ("gene," + ",".join(f"s{i}" for i in range(1, 9)) + "\n"
            "g1," + ",".join(str(i) for i in range(1, 9)) + "\n").encode()),
        "metadata": AgentInputFile("metadata.csv", ("\n".join(rows) + "\n").encode()),
    }


def _job(job_id: str = "job-1", owner_id: str = "user-1") -> JobRecord:
    now = datetime.now(timezone.utc)
    return JobRecord(
        id=job_id,
        project_name="fixture",
        analysis_type=AnalysisType.DEG,
        status=JobStatus.SUCCEEDED,
        created_at=now,
        updated_at=now,
        owner_id=owner_id,
        artifacts=[FileArtifactInfo(
            kind=FileArtifactKind.OUTPUT,
            filename="differential_gene_counts.csv",
            path="differential_gene_counts.csv",
            storage_key="fixture/result.csv",
            checksum="sha256:" + "a" * 64,
            content_type="text/csv",
            size_bytes=64,
            created_at=now,
        )],
        params={"padj_cutoff": 0.05, "same_fields": ""},
    )


def test_describe_metadata_returns_fields_levels_and_alignment() -> None:
    result = AgentToolRuntime("user-1", inputs=_inputs()).describe_metadata(
        fields=["line", "treatment"]
    )

    assert result.ok
    assert [item.field for item in result.fields] == ["line", "treatment"]
    assert result.fields[0].levels == {"WT": 6, "mutant": 2}
    assert result.fields[1].levels == {"control": 4, "salt": 4}
    assert result.alignment == {"counts": "exact"}
    assert result.sample_count == 8


def test_enumerate_contrasts_counts_each_stratum_after_alignment() -> None:
    runtime = AgentToolRuntime("user-1", inputs=_inputs())
    result = runtime.enumerate_contrasts(
        compare_field="treatment",
        scope=ScopeSpec(mode="stratified", blocking_fields=["line", "timepoint"]),
        min_replicates=2,
    )

    assert result.ok
    assert len(result.candidates) == 6
    valid = [item for item in result.candidates if item.executable]
    assert len(valid) == 2
    assert valid[0].stratum == {"line": "WT", "timepoint": "24h"}
    assert {item.tested_count for item in valid} == {2}
    assert {item.reference_count for item in valid} == {2}
    assert all(item.scope == result.scope for item in result.candidates)


def test_fixed_scope_filters_rows_and_unknown_scope_is_explicit_error() -> None:
    runtime = AgentToolRuntime("user-1", inputs=_inputs())
    fixed = runtime.enumerate_contrasts(
        compare_field="treatment",
        scope=ScopeSpec(mode="fixed", fixed_filters={"line": "WT", "timepoint": "24h"}),
    )
    unknown = runtime.enumerate_contrasts(
        compare_field="treatment",
        scope=ScopeSpec(mode="unknown"),
    )

    assert len(fixed.candidates) == 2
    assert all(item.executable for item in fixed.candidates)
    assert not unknown.ok
    assert unknown.error_code == "scope_unknown"


def test_list_jobs_and_describe_artifacts_are_owner_bound() -> None:
    jobs = _Jobs([_job(), _job("job-2", owner_id="user-2")])
    files = _Files("Gene,padj\nGeneA,0.01\n")
    runtime = AgentToolRuntime("user-1", job_store=jobs, files=files)

    listed = runtime.list_jobs(analysis_type="DEG", limit=5)
    described = runtime.describe_artifacts("job-1")
    hidden = runtime.describe_artifacts("job-2")

    assert listed.ok and [item.job_id for item in listed.jobs] == ["job-1"]
    assert described.ok
    assert described.artifacts[0].schema.columns == ["Gene", "padj"]
    assert described.artifacts[0].schema.column_types == {"Gene": "string", "padj": "number"}
    assert not hidden.ok and hidden.error_code == "not_found"


def test_query_artifact_uses_new_tool_name_without_changing_legacy_query() -> None:
    job = _job()
    runtime = AgentToolRuntime(
        "user-1",
        job_store=_Jobs([job]),
        files=_Files("Gene,padj\nGeneA,0.01\n"),
    )

    result = runtime.query_artifact("job-1", "differential_gene_counts.csv", limit=1)

    assert result.ok
    assert result.tool.value == "query_artifact"
    assert result.rows[0]["_row_id"] == 1
