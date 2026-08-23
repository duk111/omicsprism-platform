from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from backend.app.agent.grounding import EvidenceGrounder
from backend.app.agent.schemas import Citation, GroundedAnswer, GroundedClaim
from backend.app.agent.tools import AgentToolRuntime
from backend.app.agent.verifier import AnswerVerifier
from backend.app.models import (
    AnalysisType,
    FileArtifactInfo,
    FileArtifactKind,
    JobRecord,
    JobStatus,
)


PCA_PAYLOAD = {
    "figure_id": "pca",
    "plotly_spec": {
        "datasets": {
            "transcriptome": {
                "var_exp": [0.62, 0.21],
                "groups": [
                    {"sample_id": "S1", "group1": "control"},
                    {"sample_id": "S2", "group1": "salt"},
                ],
            }
        }
    },
}

PANEL_PAYLOAD = {
    "figure_id": "gene-metabolite_panels",
    "plotly_spec": {
        "panels": [
            {"id": "pair-1", "entity_id": "WRKY33", "metabolite_id": "M01", "rho": 0.71},
            {"id": "pair-2", "entity_id": "NAC1", "metabolite_id": "M02", "rho": 0.42},
        ]
    },
}

UPSET_PAYLOAD = {
    "figure_id": "association_evidence_upset",
    "upset_data": {
        "n_edges": 15,
        "intersections": [
            {"positive": True, "validated": False, "count": 4, "support": 1},
            {"positive": True, "validated": True, "count": 8, "support": 2},
            {"positive": False, "validated": True, "count": 3, "support": 1},
        ],
    },
}


class _Jobs:
    def __init__(self, job: JobRecord) -> None:
        self.job = job

    def get_for_user(self, job_id: str, user_id: str) -> JobRecord:
        if job_id != self.job.id or user_id != self.job.owner_id:
            raise HTTPException(status_code=404, detail="Job not found")
        return self.job

    def save(self, job: JobRecord) -> None:
        self.job = job


class _Files:
    def __init__(self, text: str) -> None:
        self.text = text

    def read_artifact_text(self, job_id: str, relative_path: str, *, max_chars: int | None = None) -> str:
        return self.text


def _runtime(
    payload: object,
    *,
    filename: str,
    analysis_type: AnalysisType = AnalysisType.CORRELATION,
    owner_id: str = "user-1",
    checksum: str | None = "sha256:fixture",
) -> AgentToolRuntime:
    text = json.dumps(payload, separators=(",", ":"))
    now = datetime.now(timezone.utc)
    artifact = FileArtifactInfo(
        kind=FileArtifactKind.OUTPUT,
        filename=filename,
        path=f"figure_data/{filename}",
        storage_key=f"jobs/job-1/figure_data/{filename}",
        checksum=checksum,
        size_bytes=len(text.encode()),
        created_at=now,
    )
    job = JobRecord(
        id="job-1",
        project_name="fixture",
        analysis_type=analysis_type,
        status=JobStatus.SUCCEEDED,
        created_at=now,
        updated_at=now,
        owner_id=owner_id,
        artifacts=[artifact],
    )
    return AgentToolRuntime(
        user_id="user-1",
        inputs={},
        job_store=_Jobs(job),
        files=_Files(text),
    )


def test_pca_explained_variance_scalar_lookup() -> None:
    evidence = _runtime(PCA_PAYLOAD, filename="pca.json").query_result_evidence(
        "job-1",
        "pca.json",
        field_path="plotly_spec.datasets.transcriptome.var_exp.0",
    )

    assert evidence.ok
    assert evidence.rows == [{"_row_id": 1, "value": 0.62}]


def test_json_entity_lookup_uses_real_panel_entity_field() -> None:
    evidence = _runtime(PANEL_PAYLOAD, filename="scatter-panels.json").query_result_evidence(
        "job-1",
        "scatter-panels.json",
        field_path="plotly_spec.panels",
        resolve_entity="WRKY33",
    )

    assert evidence.row_count == 1
    assert evidence.rows[0]["entity_id"] == "WRKY33"
    assert evidence.rows[0]["_row_id"] == 1


def test_json_sort_and_limit_preserve_source_row_ids() -> None:
    evidence = _runtime(UPSET_PAYLOAD, filename="upset.json").query_result_evidence(
        "job-1",
        "upset.json",
        field_path="upset_data.intersections",
        sort="count desc",
        limit=2,
    )

    assert [row["count"] for row in evidence.rows] == [8, 4]
    assert [row["_row_id"] for row in evidence.rows] == [2, 1]
    assert evidence.row_count == 3
    assert evidence.truncated


def test_json_array_filter_is_exact_and_bounded() -> None:
    evidence = _runtime(UPSET_PAYLOAD, filename="upset.json").query_result_evidence(
        "job-1",
        "upset.json",
        field_path="upset_data.intersections",
        filters={"positive": True, "validated": True},
    )

    assert evidence.rows == [
        {"_row_id": 2, "positive": True, "validated": True, "count": 8, "support": 2}
    ]


def test_json_direct_query_rejects_unbounded_filters() -> None:
    evidence = _runtime(UPSET_PAYLOAD, filename="upset.json").query_result_evidence(
        "job-1",
        "upset.json",
        field_path="upset_data.intersections",
        filters={f"field_{index}": index for index in range(9)},
    )

    assert not evidence.ok
    assert evidence.error_code == "invalid_filter"


def test_json_citation_uses_artifact_checksum_or_content_hash() -> None:
    runtime = _runtime(UPSET_PAYLOAD, filename="upset.json", checksum=None)
    evidence = runtime.query_result_evidence(
        "job-1", "upset.json", field_path="upset_data.n_edges"
    )
    expected_text = json.dumps(UPSET_PAYLOAD, separators=(",", ":"))

    assert evidence.artifact == "figure_data/upset.json"
    assert evidence.checksum == "sha256:" + hashlib.sha256(expected_text.encode()).hexdigest()
    assert evidence.rows == [{"_row_id": 1, "value": 15}]


def test_unlisted_json_artifact_is_rejected() -> None:
    result = _runtime({}, filename="secret.json").query_result_evidence(
        "job-1", "secret.json", field_path="value"
    )

    assert not result.ok
    assert result.error_code == "artifact_not_allowed"


def test_figure_json_for_wrong_analysis_type_is_rejected() -> None:
    result = _runtime(
        PCA_PAYLOAD, filename="pca.json", analysis_type=AnalysisType.DIFFERENTIAL
    ).query_result_evidence(
        "job-1", "pca.json", field_path="plotly_spec.datasets.transcriptome.var_exp"
    )

    assert not result.ok
    assert result.error_code == "artifact_not_allowed"


def test_json_evidence_passes_existing_grounder_and_verifier() -> None:
    evidence = _runtime(UPSET_PAYLOAD, filename="upset.json").query_result_evidence(
        "job-1",
        "upset.json",
        field_path="upset_data.intersections",
        filters={"validated": True},
        sort="count desc",
        limit=1,
    )
    draft = GroundedAnswer(claims=[GroundedClaim(
        text="The largest validated intersection count is 8.",
        citation=Citation(
            artifact=evidence.artifact or "",
            checksum=evidence.checksum or "",
            row_ids=[2],
        ),
    )])

    answer = EvidenceGrounder().ground(evidence, draft)
    verdict = AnswerVerifier().verify(answer, [evidence])

    assert verdict.verdict.value == "approved"
    assert verdict.checks[0].citation_valid
    assert verdict.checks[0].number_matches_evidence


def test_json_access_preserves_owner_bound_404() -> None:
    runtime = _runtime(PCA_PAYLOAD, filename="pca.json", owner_id="user-2")

    with pytest.raises(HTTPException) as exc:
        runtime.query_result_evidence("job-1", "pca.json", field_path="figure_id")

    assert exc.value.status_code == 404
