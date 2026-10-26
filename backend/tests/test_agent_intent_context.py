"""PR9: deterministic routing and real result boundaries; no live model claims."""
from __future__ import annotations

from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest

from backend.app.agent.context import ContextAssembler
from backend.app.agent.graph import AgentRole, GraphState, JobRef, PendingAnalysisClarification, RouteTarget
from backend.app.agent.nodes.main import route_node
from backend.app.agent.queue import AgentTurnInput, AgentTurnWorkItem
from backend.app.agent.router import route
from backend.app.agent.runtime import AgentRuntime
from backend.app.agent.schemas import RunFocus
from backend.app.agent.graph import build_agent_graph
from backend.app.agent.capabilities import CapabilityPrincipal, build_readonly_capability_registry
from backend.app.agent.nodes.result_qa import job_reader_from_runtime, result_querier_from_runtime
from backend.app.agent.verifier import AnswerVerifier
from backend.app.models import AnalysisType
from backend.tests.test_agent_artifact_e2e import persisted_runtime


ROUTING_CASES = [
    ("什么是火山图？", "qa", False),
    ("DEG 中 padj 表示什么？", "qa", False),
    ("What does padj mean in DEG?", "qa", False),
    ("什么是差异分析？", "qa", False),
    ("当前火山图有哪些显著基因？", "result_qa", False),
    ("当前 GMA 结果中 GeneA 关联了哪些代谢物？", "result_qa", False),
    ("帮我配置 DEG 分析。", "analysis", False),
    ("Run DEG with padj cutoff 0.01", "analysis", False),
    ("把当前待确认 GMA 计划中的 fdr_cutoff 改成 0.1。", "analysis", True),
    ("set padj cutoff to 0.1", "analysis", True),
    ("把刚才的阈值改成 0.1。", "analysis", True),
    ("取消刚才的计划", "analysis", True),
    ("它的 padj 是多少？", "result_qa", False),
    ("其中哪个 log2FoldChange 最大？", "result_qa", False),
]


def state(message, jobs=("job-a",), *, pending=False, focused=True):
    return GraphState(thread_id="thread-1", user_id="user-1", user_message=message,
        current_job=JobRef(job_id=jobs[0], owner_id="user-1") if len(jobs) == 1 and focused else None,
        recent_jobs=[JobRef(job_id=j, owner_id="user-1") for j in jobs],
        focus=RunFocus(in_scope_job_ids=list(jobs) if focused else [], resolved_entities={}, last_citation=None),
        pending_analysis=PendingAnalysisClarification(analysis_type="GMA", question="Confirm?",
            source_message="Configure GMA", missing=["confirmation"]) if pending else None)


@pytest.mark.parametrize("message,expected,pending", ROUTING_CASES)
def test_pr9_explicit_intent_matrix(message, expected, pending):
    assert route(state(message, pending=pending)).value == expected


@pytest.mark.parametrize("message", ["解释一下这个。", "看看刚才那个结果。", "继续分析刚才那个结果。", "Explain that result"])
def test_multiple_jobs_are_clarified_without_model(message):
    s = state(message, jobs=("job-a", "job-b"))
    result = route_node(s, lambda *_a, **_k: pytest.fail("ambiguous Job must not call a model"))
    assert result["route_decision"].target is RouteTarget.AMBIGUOUS
    assert "Job" in result["response_text"]
    assert not result.get("visited_roles")


def merge(s, message, focus=(), *, history=()):
    product = SimpleNamespace(list_messages=lambda **_: list(history))
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.context = SimpleNamespace(product_store=product)
    item = AgentTurnWorkItem(thread_id=s.thread_id, user_id=s.user_id, turn_id="turn-next", trace_id="trace-next",
                            input=AgentTurnInput(message=message, focus_job_ids=list(focus)))
    return runtime._merge_turn_input(item, s, "run-next")


def test_focus_switch_updates_current_job_and_forgets_old_result_context():
    s = state("query GeneA")
    s.focus.resolved_entities = {"gene": "SecretGeneA"}
    switched = merge(s, "解释一下这个", ["job-b"])
    assert switched.current_job.job_id == "job-b"
    assert switched.focus.resolved_entities == {}
    assert switched.result_selection is None
    assert switched.conversation_memory.citation_refs == []
    assert switched.recent_messages.messages == []
    assert [j.job_id for j in ContextAssembler().assemble_for_result_qa(switched).recent_jobs] == ["job-b"]


def test_explicit_focus_wins_over_stale_current_job():
    s = state("看看刚才那个结果")
    s.focus.in_scope_job_ids = ["job-b", "job-c"]
    assert route(s) is RouteTarget.AMBIGUOUS


def test_no_job_result_request_is_clarified():
    assert route(state("当前结果中哪些基因显著上调？", jobs=())) is AgentRole.RESULT_QA


DATA = (b"gene_id,log2FoldChange,pvalue,padj\n"
        b"GeneA,1,0.001,0.001\nGeneB,5,0.002,0.002\nGeneC,3,0.003,0.003\n"
        b"GeneD,2,0.004,0.004\nGeneE,4,0.005,0.005\nGeneZ,99,0.9,0.9\n")


def conversation(tmp_path, entry="tool_call", *, content=DATA, analysis_type=AnalysisType.DEG):
    runtime, job = persisted_runtime(tmp_path, analysis_type, content, "salt.all.csv")
    registry = build_readonly_capability_registry(runtime)
    requests, roles = [], []

    def execute(request, graph_state):
        requests.append(request)
        return registry.invoke(request.tool.value, request.arguments,
            principal=CapabilityPrincipal(subject=graph_state.user_id, transport="internal"))

    class Model:
        outputs = []
        def __call__(self, context, *, role):
            roles.append(role.value)
            return self.outputs.pop(0)

    model = Model()
    graph = build_agent_graph(model, lambda *_: [], lambda *_: pytest.fail("query must never submit a Job"),
        job_reader_from_runtime(runtime), result_querier_from_runtime(runtime), tool_executor=execute)
    def first(filters=None, *, limit=5):
        query = {"artifact": "outputs/salt.all.csv", "filters": filters or {}, "sort": "padj asc", "limit": limit}
        if entry == "tool_call":
            model.outputs = [
                {"decision": {"action": "tool_call", "tool": "query_artifact", "arguments": {"job_id": job.id, **query}}},
                {"decision": {"action": "answer"}, "answer": "GeneZ is the best."},
            ]
        else:
            model.outputs = [{"decision": {"action": "query_result", "result_query": query}}]
        s = state("当前 DEG 结果中 padj 最小的 5 个基因是什么？", jobs=(job.id,))
        return GraphState.model_validate(graph.invoke(s, {"configurable": {"thread_id": s.thread_id}}))
    return runtime, job, graph, model, requests, roles, first


@pytest.mark.parametrize("entry", ["tool_call", "query_result"])
def test_followup_requeries_exact_previous_five_and_compares_actual_evidence(tmp_path, entry):
    runtime, job, graph, model, requests, roles, first = conversation(tmp_path, entry)
    previous = first()
    # Exercise the actual serialized checkpoint contract, including owner_id.
    previous = GraphState.model_validate(previous.model_dump(mode="json"))
    assert previous.result_selection.row_ids == [1, 2, 3, 4, 5]
    follow = merge(previous, "其中哪个 log2FoldChange 最大？")
    model.outputs = [{"decision": {"action": "grounded_answer", "grounded_answer": {
        "claims": [{"text": "GeneB has the largest log2FoldChange 5", "citation": {
            "artifact": "outputs/salt.all.csv", "checksum": previous.result_selection.checksum, "row_ids": [2]}}]}}}]
    count = len(roles)
    result = GraphState.model_validate(graph.invoke(follow, {"configurable": {"thread_id": follow.thread_id}}))
    assert len(roles) == count, "a deterministic comparison needs no additional model"
    assert requests[-1].arguments["limit"] == 5
    assert requests[-1].arguments["sort"] == "padj asc"
    assert "GeneB" in result.response_text
    assert "GeneZ" not in result.response_text
    assert result.grounded_answer.claims[-1].citation.row_ids == [2]
    evidence = runtime.query_result(job.id, "outputs/salt.all.csv", sort="padj asc", limit=5)
    assert AnswerVerifier().verify(result.grounded_answer, [evidence]).verdict.value == "approved"


@pytest.mark.parametrize("question", ["它的 padj 是多少？", "What is its padj?"])
@pytest.mark.parametrize("entry", ["tool_call", "query_result"])
def test_gene_followup_is_requeried_and_not_taken_from_model_history(tmp_path, entry, question):
    runtime, job, graph, model, requests, roles, first = conversation(tmp_path, entry)
    previous = first({"gene_id": "GeneA"}, limit=1)
    follow = merge(previous, question)
    model.outputs = [{"decision": {"action": "grounded_answer", "grounded_answer": {
        "claims": [{"text": "GeneA has padj 0.001", "citation": {
            "artifact": "outputs/salt.all.csv", "checksum": previous.result_selection.checksum, "row_ids": [1]}}]}}}]
    result = GraphState.model_validate(graph.invoke(follow, {"configurable": {"thread_id": follow.thread_id}}))
    assert requests[-1].arguments["filters"] == {"gene_id": "GeneA"}
    assert "GeneA" in result.response_text and "0.001" in result.response_text
    assert "GeneZ" not in result.response_text


@pytest.mark.parametrize("change", ["checksum", "deleted", "permission"])
def test_old_selection_cannot_answer_after_artifact_or_access_changes(tmp_path, change):
    runtime, job, graph, model, requests, roles, first = conversation(tmp_path)
    previous = first({"gene_id": "GeneA"}, limit=1)
    if change == "checksum":
        runtime.files.output_path(job.id, "salt.all.csv").write_bytes(DATA.replace(b"GeneA,1", b"GeneA,9"))
        job.artifacts = runtime.files.sync_workspace_artifacts(job)
        runtime.job_store.save(job)
    elif change == "permission":
        job.owner_id = "another-user"
        runtime.job_store.save(job)
    else:
        runtime.files.output_path(job.id, "salt.all.csv").unlink()
    model.outputs = [{"decision": {"action": "answer"}, "answer": "GeneA has padj 0.001"}]
    result = GraphState.model_validate(graph.invoke(merge(previous, "它的 padj 是多少？"),
        {"configurable": {"thread_id": previous.thread_id}}))
    assert result.grounded_answer is None
    assert result.result_selection is None
    assert "GeneA" not in result.response_text
    assert len(roles) == 2


def test_focus_switch_history_stays_isolated_on_later_turn_and_on_store_failure():
    now = datetime.now(timezone.utc)
    def record(text, when):
        return SimpleNamespace(role="assistant", message_id=text, created_at=when, blocks=[SimpleNamespace(text=text)])
    old = record("SecretGeneA padj=0.001", now - timedelta(days=1))
    switched = merge(state("previous"), "query job B", ["job-b"], history=[old])
    next_state = merge(switched, "继续查看", history=[old, record("GeneB", now + timedelta(days=1))])
    assert "SecretGeneA" not in next_state.model_dump_json()
    runtime = AgentRuntime.__new__(AgentRuntime)
    def fail(**_):
        raise RuntimeError("store unavailable")
    runtime.context = SimpleNamespace(product_store=SimpleNamespace(list_messages=fail))
    item = AgentTurnWorkItem(thread_id="thread-1", user_id="user-1", turn_id="switch",
        input=AgentTurnInput(message="new job", focus_job_ids=["job-c"]))
    result = runtime._merge_turn_input(item, next_state, "run")
    assert "GeneB" not in result.model_dump_json()


@pytest.mark.parametrize("message", ["什么是火山图？", "DEG 中 padj 表示什么？"])
def test_scientific_history_does_not_override_explicit_knowledge_intent(message):
    from backend.app.agent.context import RecentMessages, RecentMessage
    s = state(message).model_copy(update={"recent_messages": RecentMessages(context_version="test",
        messages=[RecentMessage(role="assistant", turn_id="old", text="gene_id=GeneA; padj=0.001")])})
    assert route(s) is AgentRole.QA


@pytest.mark.parametrize("analysis_type,content,filters,question,expected", [
    (AnalysisType.DEM, DATA.replace(b"gene_id", b"metabolite_id").replace(b"Gene", b"Met").replace(b"padj", b"padj_bh"),
     {"metabolite_id": "MetA"}, "它的 padj_bh 是多少？", "MetA"),
    (AnalysisType.GMA, b"Gene,Metabolite,PearsonR,FDR\nGeneA,MetA,0.9,0.01\nGeneA,MetB,0.7,0.02\nGeneZ,MetZ,0.99,0.001\n",
     {"Gene": "GeneA"}, "其中哪个 PearsonR 最大？", "MetA"),
])
def test_dem_gma_followup_preserves_query_and_original_citation(tmp_path, analysis_type, content, filters, question, expected):
    from backend.app.agent.context import ResultSelection
    runtime, job = persisted_runtime(tmp_path, analysis_type, content,
        "T02_High_Confidence_Network.csv" if analysis_type is AnalysisType.GMA else "salt.all.csv")
    artifact = "outputs/" + ("T02_High_Confidence_Network.csv" if analysis_type is AnalysisType.GMA else "salt.all.csv")
    source = runtime.query_result(job.id, artifact, filters=filters)
    from backend.app.agent.result_context import selection_from_evidence
    selection = selection_from_evidence("user-1", {"job_id": job.id, "artifact": artifact, "filters": filters}, source)
    registry = build_readonly_capability_registry(runtime)
    requests = []
    def execute(request, s):
        requests.append(request)
        return registry.invoke(request.tool.value, request.arguments, principal=CapabilityPrincipal(subject=s.user_id, transport="internal"))
    graph = build_agent_graph(lambda *_a, **_k: pytest.fail("followup needs no model"), lambda *_: [], lambda *_: None,
        job_reader_from_runtime(runtime), result_querier_from_runtime(runtime), tool_executor=execute)
    s = state(question, jobs=(job.id,)).model_copy(update={"result_selection": selection})
    result = GraphState.model_validate(graph.invoke(s, {"configurable": {"thread_id": s.thread_id}}))
    assert expected in result.response_text
    assert "GeneZ" not in result.response_text
    assert requests[0].arguments["filters"] == filters
    assert AnswerVerifier().verify(result.grounded_answer, [source]).verdict.value == "approved"


@pytest.mark.parametrize("entry", ["tool_call", "query_result"])
def test_switch_rejects_model_request_for_old_job(tmp_path, entry):
    runtime, job, graph, model, requests, roles, first = conversation(tmp_path, entry)
    previous = first({"gene_id": "GeneA"}, limit=1)
    switched = merge(previous, "查询当前结果", ["job-b"])
    old = ({"action": "tool_call", "tool": "query_artifact", "arguments": {
        "job_id": job.id, "artifact": "outputs/salt.all.csv"}} if entry == "tool_call" else
        {"action": "query_result", "job_id": job.id, "result_query": {"artifact": "outputs/salt.all.csv"}})
    model.outputs = [{"decision": old}, {"decision": {"action": "answer"}, "answer": "GeneA has padj 0.001"}]
    count = len(requests)
    result = GraphState.model_validate(graph.invoke(switched, {"configurable": {"thread_id": switched.thread_id}}))
    assert result.grounded_answer is None
    assert "GeneA" not in result.response_text
    assert len(requests) == count


def test_pending_gma_threshold_pronoun_edits_plan_without_submission():
    from backend.app.agent.nodes.analysis_agent import analysis_agent_node
    from backend.app.agent.clarification_resolver import ClarificationResolver
    from backend.app.agent.param_resolver import AnalysisProposal
    s = state("把刚才的阈值改成 0.1。", pending=True)
    s.pending_analysis.proposal = AnalysisProposal(analysis_type="GMA", requested_params={"fdr_cutoff": 0.05})
    result = analysis_agent_node(lambda *_a, **_k: pytest.fail("deterministic edit should not call model"),
        clarification_resolver=ClarificationResolver())(s)
    assert result["decision"].action == "propose_plan"
    assert result["decision"].proposal.requested_params["fdr_cutoff"] == 0.1
    assert result["pending_analysis"].status == "active"
    assert result.get("current_job") is None


def test_pending_degenerate_threshold_is_clarified_without_guessing():
    from backend.app.agent.nodes.analysis_agent import analysis_agent_node
    from backend.app.agent.clarification_resolver import ClarificationResolver
    from backend.app.agent.param_resolver import AnalysisProposal
    s = state("把刚才的阈值改成 0.1。", pending=True)
    s.pending_analysis.analysis_type = "DEG"
    s.pending_analysis.proposal = AnalysisProposal(analysis_type="DEG", requested_params={"padj_cutoff": 0.05, "log2fc_cutoff": 1})
    result = analysis_agent_node(lambda *_a, **_k: pytest.fail("do not guess"), clarification_resolver=ClarificationResolver())(s)
    assert result["decision"].action == "ask_user"
    assert result["pending_analysis"].proposal.requested_params == {"padj_cutoff": 0.05, "log2fc_cutoff": 1}


@pytest.mark.parametrize("scope", [[], ["job-a", "job-b"]])
def test_unique_focus_is_required_even_with_historical_selection(scope):
    from backend.app.agent.context import ResultSelection
    s = state("解释一下这个", jobs=("job-a", "job-b"), focused=False)
    s.focus.in_scope_job_ids = scope
    assert route(s) is RouteTarget.AMBIGUOUS


def test_other_user_selection_cannot_roundtrip_in_graph_state():
    from backend.app.agent.context import ResultSelection
    s = state("它的 padj？").model_dump(mode="json")
    s["result_selection"] = ResultSelection(job_id="job-a", owner_id="other-user", artifact="x.csv",
        checksum="x", arguments={"job_id": "job-a", "artifact": "x.csv"}, row_ids=[1]).model_dump(mode="json")
    with pytest.raises(ValueError, match="selection must belong"):
        GraphState.model_validate(s)
