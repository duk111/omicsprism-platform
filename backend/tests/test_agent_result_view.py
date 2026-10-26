from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.app.agent.context import ContextAssembler, RecentMessage, RecentMessages, ResultSelection
from backend.app.agent.graph import GraphState, JobRef
from backend.app.agent.queue import AgentTurnInput, AgentTurnWorkItem
from backend.app.agent.runtime import AgentRuntime
from backend.app.agent.schemas import ResultViewReference, RunFocus
from backend.app.agent.view_context import validate_result_view
from backend.app.models import AnalysisType
from backend.tests.test_agent_api import _client, _context, _create_thread
from backend.tests.test_agent_artifact_e2e import DEG, persisted_runtime


def view(job, **changes):
    return ResultViewReference(job_id=job.id, analysis_type=job.analysis_type.value.upper(),
                               result_type="table", artifact="outputs/salt.all.csv").model_copy(update=changes)


def test_api_validates_view_and_queues_reference_without_rewriting_question(tmp_path):
    runtime, job = persisted_runtime(tmp_path, AnalysisType.DEG, DEG, "salt.all.csv", owner="user-a")
    context = _context()
    # Preserve the real repository boundary, replacing only the test context dependency.
    object.__setattr__(context, "job_store", runtime.job_store)
    client = _client(context)
    thread = _create_thread(client)
    reference = view(job).model_dump()
    response = client.post(f"/api/agent/threads/{thread['thread_id']}/turns",
                           headers={"Idempotency-Key": "view-1"},
                           json={"message": "当前任务中 padj 最小的基因", "focus_job_ids": [job.id], "result_view": reference})
    assert response.status_code == 202, response.text
    queued = AgentTurnWorkItem.model_validate_json(context.turn_queue.reserve())
    assert queued.input.result_view.model_dump() == reference
    assert queued.input.message == "当前任务中 padj 最小的基因"
    assert queued.input.focus_job_ids == [job.id]


@pytest.mark.parametrize("change,status", [
    ({"artifact": "../../secret.csv"}, 404),
    ({"artifact": "C:/secret.csv"}, 404),
    ({"artifact": "outputs/unregistered.csv"}, 404),
    ({"job_id": "other-job"}, 422),
    ({"analysis_type": "GMA"}, 422),
])
def test_api_rejects_forged_view_before_enqueue(tmp_path, change, status):
    runtime, job = persisted_runtime(tmp_path, AnalysisType.DEG, DEG, "salt.all.csv", owner="user-a")
    context = _context()
    object.__setattr__(context, "job_store", runtime.job_store)
    client = _client(context)
    thread = _create_thread(client)
    response = client.post(f"/api/agent/threads/{thread['thread_id']}/turns",
                           headers={"Idempotency-Key": "invalid-view"},
                           json={"message": "查询当前结果", "focus_job_ids": [job.id],
                                 "result_view": view(job, **change).model_dump()})
    assert response.status_code == status
    assert context.turn_queue.reserve() is None


@pytest.mark.parametrize("analysis_type", list(AnalysisType))
def test_view_accepts_registered_owned_artifact_for_each_analysis(tmp_path, analysis_type):
    runtime, job = persisted_runtime(tmp_path, analysis_type, DEG, "salt.all.csv")
    assert validate_result_view(runtime.job_store, view(job), [job.id], "user-1") == view(job)


@pytest.mark.parametrize("invalidation", ["owner", "deleted", "missing", "artifact"])
def test_execution_revalidates_context_after_enqueue(tmp_path, invalidation):
    runtime, job = persisted_runtime(tmp_path, AnalysisType.DEG, DEG, "salt.all.csv")
    worker = AgentRuntime.__new__(AgentRuntime)
    worker.context = SimpleNamespace(job_store=runtime.job_store)
    item = AgentTurnWorkItem(thread_id="thread-1", turn_id="turn-1", user_id="user-1",
                             input=AgentTurnInput(message="查询当前结果", focus_job_ids=[job.id], result_view=view(job)))
    if invalidation == "owner":
        job.owner_id = "another-user"
    elif invalidation == "deleted":
        job.deleted_at = datetime.now(timezone.utc)
    elif invalidation == "artifact":
        job.artifacts = []
        job.result_files = []
    else:
        item.input.result_view.job_id = "missing"
        item.input.focus_job_ids = ["missing"]
    runtime.job_store.save(job)
    with pytest.raises(HTTPException) as error:
        worker._merge_turn_input(item, None, "run-1")
    assert error.value.status_code == 404
    if invalidation in {"owner", "deleted"}:
        with pytest.raises(HTTPException):
            runtime.query_artifact(job.id, "outputs/salt.all.csv")


def test_view_change_preserves_chat_but_resets_old_query_locator(tmp_path):
    runtime, job = persisted_runtime(tmp_path, AnalysisType.DEG, DEG, "salt.all.csv")
    history = RecentMessages(context_version="history", messages=[RecentMessage(role="user", turn_id="old", text="Old question")])
    selection = ResultSelection(job_id=job.id, owner_id="user-1", artifact="outputs/salt.all.csv",
                                checksum="old", arguments={}, row_ids=[1])
    state = GraphState(thread_id="thread-1", user_id="user-1", user_message="old",
                       current_job=JobRef(job_id=job.id, owner_id="user-1"),
                       focus=RunFocus(in_scope_job_ids=[job.id], resolved_entities={"gene": "old"}, last_citation=None),
                       result_view=view(job, result_type="overview", artifact=None), result_selection=selection,
                       recent_messages=history)
    worker = AgentRuntime.__new__(AgentRuntime)
    worker.context = SimpleNamespace(job_store=runtime.job_store, product_store=None)
    item = AgentTurnWorkItem(thread_id="thread-1", turn_id="new", user_id="user-1",
                             input=AgentTurnInput(message="查询当前结果", focus_job_ids=[job.id], result_view=view(job)))
    switched = worker._merge_turn_input(item, state, "run-1")
    assert switched.result_selection is None
    assert switched.focus.resolved_entities == {}
    assert switched.recent_messages == history
    assert ContextAssembler().assemble_for_result_qa(switched).result_view == view(job)
    # A repeated view retains PR 9 follow-up selection; general chat drops the UI locator.
    same = switched.model_copy(update={"result_selection": selection})
    assert worker._merge_turn_input(item, same, "run-1").result_selection == selection
    item.input.result_view = None
    assert worker._merge_turn_input(item, same, "run-1").result_view is None


def test_assembler_does_not_project_a_view_from_another_job():
    state = GraphState(thread_id="thread", user_id="user", user_message="查询当前结果",
                       focus=RunFocus(in_scope_job_ids=["job-b"], resolved_entities={}, last_citation=None),
                       result_view=ResultViewReference(job_id="job-a", analysis_type="DEG", result_type="overview"))
    assert ContextAssembler().assemble_for_result_qa(state).result_view is None


@pytest.mark.parametrize("entry", ["tool_call", "query_result"])
def test_selected_table_cannot_be_replaced_by_a_model_query(tmp_path, entry):
    from backend.app.agent.graph import build_agent_graph
    from backend.app.agent.nodes.result_qa import job_reader_from_runtime, result_querier_from_runtime
    from backend.tests.test_agent_artifact_e2e import ScriptedModel

    runtime, job = persisted_runtime(tmp_path, AnalysisType.DEG, DEG, "salt.all.csv")
    queries = []
    def forbidden_query(*args):
        queries.append(args)
        pytest.fail("A different table must be rejected before any evidence read")
    decision = ({"action": "tool_call", "tool": "query_artifact", "arguments": {
        "job_id": job.id, "artifact": "outputs/other.all.csv"}} if entry == "tool_call" else
        {"action": "query_result", "result_query": {"artifact": "outputs/other.all.csv"}})
    graph = build_agent_graph(ScriptedModel({"decision": decision}, {"decision": {"action": "ask_user", "question": "Select a table"}}),
                              lambda *_: [], lambda *_: None, job_reader_from_runtime(runtime),
                              forbidden_query, tool_executor=forbidden_query)
    state = GraphState(thread_id="view-bound", user_id="user-1", user_message="查询当前结果",
                       current_job=JobRef(job_id=job.id, owner_id="user-1"), result_view=view(job))
    result = GraphState.model_validate(graph.invoke(state, {"configurable": {"thread_id": state.thread_id}}))
    assert queries == []
    assert result.grounded_answer is None
    assert not any(block.type == "evidence" for block in result.response_blocks)
