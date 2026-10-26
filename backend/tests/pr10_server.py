"""Isolated browser acceptance server: production APIs/graph/tools, Scripted Model.

Run only via playwright.pr10.config.ts. Uses temporary storage and retained real
Worker output bytes; no LLM, Redis, PostgreSQL, scientific execution or secrets.
"""
from __future__ import annotations

import asyncio
import csv
import io
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI
from langgraph.checkpoint.memory import InMemorySaver

from backend.app.agent.api import create_agent_router
from backend.app.agent.bootstrap import AgentApiContext
from backend.app.agent.capabilities import CapabilityPrincipal, build_readonly_capability_registry
from backend.app.agent.graph import build_agent_graph
from backend.app.agent.nodes.result_qa import job_reader_from_runtime, result_querier_from_runtime
from backend.app.agent.product_store import InMemoryAgentProductStore
from backend.app.agent.queue import InMemoryAgentTurnQueue
from backend.app.agent.runtime import AgentRuntime
from backend.app.agent.tools import AgentToolRuntime
from backend.app.models import AnalysisType, JobRecord, JobStatus, ResultFileInfo
from backend.tests.test_agent_artifact_e2e import ScriptedModel

workspace = TemporaryDirectory(prefix="omicsprism-pr10-")
os.environ["OMICS_PRISM_RUNS_DIR"] = str(Path(workspace.name) / "runs")
os.environ["OMICS_PRISM_AUTH_DIR"] = str(Path(workspace.name) / "auth")
os.environ["OMICS_PRISM_FILE_STORAGE_ROOT"] = str(Path(workspace.name) / "storage")
os.environ["OMICS_PRISM_STORAGE_BACKEND"] = "json"
os.environ["OMICS_PRISM_FILE_STORAGE_BACKEND"] = "local"
os.environ.pop("OMICS_PRISM_AGENT_MODEL_URL", None)
from backend.app import main  # noqa: E402 -- storage isolation must precede app bootstrap

OWNER = "pr10-browser-user"
FIXTURES = Path(__file__).parents[1] / "app/agent/fixtures/result_query"
now = datetime.now(timezone.utc)
for analysis in AnalysisType:
    job = JobRecord(id=f"pr10-{analysis.value}", owner_id=OWNER, project_name="PR 10 acceptance",
                    analysis_type=analysis, status=JobStatus.SUCCEEDED, created_at=now, updated_at=now)
    main.JOB_STORE.save(job)
    filename = "T02_High_Confidence_Network.csv" if analysis is AnalysisType.GMA else f"{analysis.value}.all.csv"
    content = (FIXTURES / filename).read_bytes()
    path = main.FILES.output_path(job.id, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    svg = main.FILES.output_path(job.id, "volcano.svg")
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="800" height="500"><rect width="800" height="500" fill="#f4faf7"/><text x="30" y="50" font-size="24">DEG Volcano Plot (UI fixture)</text></svg>', encoding="utf-8")
    job.artifacts = main.FILES.sync_workspace_artifacts(job)
    table_artifact = next(item for item in job.artifacts if item.path == f"outputs/{filename}")
    job.result_files = [ResultFileInfo(**table_artifact.model_dump(), name=filename,
                                      download_url=f"/api/jobs/{job.id}/files/outputs/{filename}")]
    main.JOB_STORE.save(job)

deleted = main.JOB_STORE.get_for_user("pr10-deg", OWNER).model_copy(update={"id": "pr10-deleted", "deleted_at": now})
main.JOB_STORE.save(deleted)

tool_runtime = AgentToolRuntime(user_id=OWNER, job_store=main.JOB_STORE, files=main.FILES)
registry = build_readonly_capability_registry(tool_runtime)
deg_job = main.JOB_STORE.get_for_user("pr10-deg", OWNER)
artifact = next(item for item in deg_job.artifacts if item.path == "outputs/deg.all.csv")
rows = list(csv.DictReader(io.StringIO((FIXTURES / "deg.all.csv").read_text(encoding="utf-8"))))
best = min(enumerate(rows, 1), key=lambda item: float(item[1]["padj"]))
claim = {"text": f"{best[1]['gene_id']} has padj {best[1]['padj']}",
         "citation": {"artifact": artifact.path, "checksum": artifact.checksum, "row_ids": [best[0]]}}
query = {"decision": {"action": "tool_call", "tool": "query_artifact", "arguments": {
    "job_id": deg_job.id, "artifact": artifact.path, "sort": "padj asc", "limit": 1}}}
answer = {"decision": {"action": "grounded_answer", "grounded_answer": {"claims": [claim]}}}
model = ScriptedModel(*([query, answer] * 100))
observations: list[dict] = []


def execute(request, state):
    result = registry.invoke(request.tool.value, request.arguments,
                              principal=CapabilityPrincipal(subject=state.user_id, transport="internal"))
    observations.append({"thread_id": state.thread_id, "tool": request.tool.value,
                         "arguments": request.arguments, "result": result.model_dump(mode="json"),
                         "view": state.result_view.model_dump() if state.result_view else None})
    return result


def no_execution(*args, **kwargs):
    raise AssertionError("Browser result QA must never execute an analysis")


graph = build_agent_graph(model, lambda *_: [], no_execution,
                           job_reader_from_runtime(tool_runtime), result_querier_from_runtime(tool_runtime),
                           tool_executor=execute, checkpointer=InMemorySaver())
queue = InMemoryAgentTurnQueue()
context = AgentApiContext(product_store=InMemoryAgentProductStore(), job_store=main.JOB_STORE,
                          files=main.FILES, graph=graph, turn_queue=queue, stream_poll_seconds=0.05)
runtime = AgentRuntime(context, queue)


@asynccontextmanager
async def lifespan(_app):
    async def drain():
        while True:
            raw = queue.reserve()
            if raw is not None:
                await asyncio.to_thread(runtime.run_once, raw)
            await asyncio.sleep(0.03)
    task = asyncio.create_task(drain())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    workspace.cleanup()


app = FastAPI(lifespan=lifespan)
app.include_router(create_agent_router(context=context, session_dependency=main.get_session_id))


@app.get("/__test__/evidence")
def evidence():
    return {"observations": observations, "expected_claim": claim,
            "provenance": "retained real Worker artifact; Scripted Model, not a live LLM"}


app.mount("/", main.app)
