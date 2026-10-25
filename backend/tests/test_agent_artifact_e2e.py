"""Real disk/repository/runtime/graph, Scripted Model; no model or queue service.

DEG/DEM data are schema-faithful synthetic rows. GMA reuses a captured real
Worker artifact, whose provenance/checksum is retained in fixtures/gma/manifest.json.
The assertion helper is also called by the real Worker integration tests.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

import pytest
from fastapi import HTTPException

from backend.app.agent.capabilities import CapabilityPrincipal, build_readonly_capability_registry
from backend.app.agent.graph import GraphState, JobRef, build_agent_graph
from backend.app.agent.nodes.result_qa import job_reader_from_runtime, result_querier_from_runtime
from backend.app.agent.schemas import AgentEvidenceBlock
from backend.app.agent.tools import AgentToolRuntime
from backend.app.agent.verifier import AnswerVerifier
from backend.app.job_store import JobStorageService, LocalJsonJobRepository
from backend.app.models import AnalysisType, JobRecord, JobStatus
from backend.app.settings import AppSettings
from backend.app.storage_service import FileStorageService


FIXTURES = Path(__file__).parents[1] / "app" / "agent" / "fixtures"
DEG = b"gene_id,log2FoldChange,pvalue,padj,comparison\nGeneA,1.2,0.0001,0.001,salt_vs_control\nGeneZ,-2.4,0.02,0.03,salt_vs_control\n"
DEM = b"metabolite_id,vip,log2FoldChange,pvalue,padj_bh,dem_status\nMetA,1.8,1.2,0.0001,0.001,significant\nMetB,1.6,-2.4,0.02,0.03,significant\n"


class ScriptedModel:
    def __init__(self, *outputs):
        self.outputs = list(outputs)

    def __call__(self, context, *, role):
        return self.outputs.pop(0)


def persisted_runtime(tmp_path, analysis_type, content, artifact, owner="user-1"):
    settings = AppSettings(runs_dir=tmp_path / "runs")
    jobs = JobStorageService(LocalJsonJobRepository(settings.runs_dir))
    files = FileStorageService(settings)
    files.attach_job_store(jobs)
    now = datetime.now(timezone.utc)
    job = JobRecord(id="artifact-job", owner_id=owner, project_name="qa-evidence",
                    analysis_type=analysis_type, status=JobStatus.SUCCEEDED,
                    created_at=now, updated_at=now)
    jobs.save(job)
    path = files.output_path(job.id, artifact)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    job.artifacts = files.sync_workspace_artifacts(job)
    jobs.save(job)
    return AgentToolRuntime(user_id="user-1", job_store=jobs, files=files), job


def invoke(runtime, job, *outputs, user_message="Show current job result"):
    registry = build_readonly_capability_registry(runtime)
    def execute(request, state):
        return registry.invoke(request.tool.value, request.arguments,
                               principal=CapabilityPrincipal(subject=state.user_id, transport="internal"))
    graph = build_agent_graph(ScriptedModel(*outputs), lambda *_: [], lambda *_: None,
                             job_reader_from_runtime(runtime), result_querier_from_runtime(runtime),
                             tool_executor=execute)
    result = graph.invoke(GraphState(thread_id="disk-e2e", user_id="user-1",
                                    user_message=user_message,
                                    current_job=JobRef(job_id=job.id, owner_id="user-1")),
                          {"configurable": {"thread_id": "disk-e2e"}})
    return GraphState.model_validate(result)


def assert_copilot_artifact(tmp_path, analysis_type, artifact_path, entity_field, statistic):
    """Query actual bytes through production boundaries and assert exact row binding."""
    content = artifact_path.read_bytes()
    rows = list(csv.DictReader(io.StringIO(content.decode("utf-8"))))
    assert rows, "Worker acceptance fixture must include result rows"
    row = next(row for row in rows if row.get(statistic) not in {None, "", "NA", "nan"})
    entity, value = row[entity_field], row[statistic]
    runtime, job = persisted_runtime(tmp_path, analysis_type, content, artifact_path.name)
    path = "outputs/" + artifact_path.name
    described = runtime.describe_artifacts(job.id)
    assert described.ok
    artifact = next(a for a in described.artifacts if a.path == path)
    assert {entity_field, statistic} <= set(artifact.schema.columns)
    checksum = artifact.checksum
    assert checksum.removeprefix("sha256:") == sha256(content).hexdigest()
    result = invoke(runtime, job,
                    {"decision": {"action": "tool_call", "tool": "query_artifact", "arguments": {
                        "job_id": job.id, "artifact": path, "filters": {entity_field: entity}, "limit": 1}}},
                    {"decision": {"action": "grounded_answer", "grounded_answer": {"claims": [{
                        "text": f"{entity} has {statistic} {value}",
                        "citation": {"artifact": path, "checksum": checksum,
                                     "row_ids": [rows.index(row) + 1]}}]}}})
    assert result.grounded_answer is not None
    assert result.grounded_answer.claims[0].text == f"{entity} has {statistic} {value}"
    source = runtime.query_result(job.id, path, filters={entity_field: entity}, limit=1)
    assert AnswerVerifier().verify(result.grounded_answer, [source]).verdict.value == "approved"
    assert source.rows[0][entity_field] == entity
    assert source.rows[0][statistic] == value
    assert any(isinstance(b, AgentEvidenceBlock) for b in result.response_blocks)
    return result


@pytest.mark.parametrize("analysis_type,content,artifact,field,statistic", [
    (AnalysisType.DEG, DEG, "salt.all.csv", "gene_id", "padj"),
    (AnalysisType.DEM, DEM, "salt.all.csv", "metabolite_id", "padj_bh"),
])
def test_core_output_schema_to_copilot(tmp_path, analysis_type, content, artifact, field, statistic):
    source = tmp_path / artifact
    source.write_bytes(content)
    assert_copilot_artifact(tmp_path, analysis_type, source, field, statistic)


def test_captured_real_gma_artifact_to_copilot(tmp_path):
    source = FIXTURES / "gma" / "T02_High_Confidence_Network.csv"
    manifest = json.loads((source.parent / "manifest.json").read_text())
    # The uploaded Windows checkout has CRLF; the captured manifest hashed LF.
    assert "sha256:" + sha256(source.read_bytes().replace(b"\r\n", b"\n")).hexdigest() == manifest["artifacts"][0]["fixture_checksum"]
    assert_copilot_artifact(tmp_path, AnalysisType.GMA, source, "Gene", "PearsonR")


@pytest.mark.parametrize("action", ["tool_call", "query_result", "get_job"])
def test_other_users_persisted_job_never_yields_claims(tmp_path, action):
    runtime, job = persisted_runtime(tmp_path, AnalysisType.DEG, DEG, "salt.all.csv", owner="user-2")
    with pytest.raises(HTTPException) as error:
        runtime.query_artifact(job.id, "outputs/salt.all.csv")
    assert error.value.status_code == 404
    decision = ({"action": action, "tool": "query_artifact", "arguments": {
        "job_id": job.id, "artifact": "outputs/salt.all.csv"}} if action == "tool_call" else
        {"action": action, "job_id": job.id})
    if action == "query_result":
        decision["result_query"] = {"artifact": "outputs/salt.all.csv"}
    result = invoke(runtime, job, {"decision": decision},
                    {"decision": {"action": "answer"}, "answer": "GeneZ has padj 0.001"})
    assert result.grounded_answer is None
    assert "GeneZ" not in result.response_text


def test_empty_table_refuses_model_conclusion(tmp_path):
    runtime, job = persisted_runtime(tmp_path, AnalysisType.DEM, DEM.splitlines()[0] + b"\n", "salt.all.csv")
    result = invoke(runtime, job,
                    {"decision": {"action": "tool_call", "tool": "query_artifact", "arguments": {
                        "job_id": job.id, "artifact": "outputs/salt.all.csv"}}},
                    {"decision": {"action": "answer"}, "answer": "MetB has padj_bh 0.001"})
    assert result.response_text == "没有满足阈值的证据"
    assert result.grounded_answer.claims[0].citation.row_ids == []


def test_persisted_job_status_is_still_deterministic(tmp_path):
    runtime, job = persisted_runtime(tmp_path, AnalysisType.DEG, DEG, "salt.all.csv")
    result = invoke(runtime, job, {"decision": {"action": "get_job"}})
    assert result.job_summary.status == "succeeded"
    assert result.response_text.startswith(f"Job {job.id}: succeeded")
    assert result.grounded_answer is None


@pytest.mark.parametrize("analysis_type,content,field,statistic", [
    (AnalysisType.DEG, DEG, "gene_id", "padj"),
    (AnalysisType.DEM, DEM, "metabolite_id", "padj_bh"),
])
def test_semantic_result_query_uses_actual_core_field_names(tmp_path, analysis_type, content, field, statistic):
    runtime, job = persisted_runtime(tmp_path, analysis_type, content, "salt.all.csv")
    entity = "GeneA" if analysis_type is AnalysisType.DEG else "MetA"
    result = invoke(runtime, job, {"decision": {"action": "query_result", "result_query": {
        "artifact": "outputs/salt.all.csv", "filters": {field: entity}, "sort": statistic + " asc", "limit": 1}}})
    assert f"{field}={entity}" in result.response_text
    assert f"{statistic}=0.001" in result.response_text
    assert result.grounded_answer.claims[0].citation.row_ids == [1]


def test_unregistered_artifact_cannot_become_evidence(tmp_path):
    runtime, job = persisted_runtime(tmp_path, AnalysisType.DEG, DEG, "salt.all.csv")
    result = invoke(runtime, job,
                    {"decision": {"action": "tool_call", "tool": "query_artifact", "arguments": {
                        "job_id": job.id, "artifact": "outputs/missing.all.csv"}}},
                    {"decision": {"action": "answer"}, "answer": "GeneZ has padj 0.001"})
    assert result.grounded_answer is None
    assert result.failure_code == "result_evidence_required"
    assert "GeneZ" not in result.response_text
