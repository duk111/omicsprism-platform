"""Adversarial regression tests: model text is never a result data boundary."""
from __future__ import annotations

import pytest

from backend.app.agent.context import RecentMessage, RecentMessages
from backend.app.agent.graph import AgentDecision, AgentRole, GraphState, JobRef, JobSummary, PendingAnalysisClarification, ResultQuerySpec, build_agent_graph
from backend.app.agent.grounding import GroundedAnswerPipeline
from backend.app.agent.nodes.result_qa_agent import result_qa_agent_node
from backend.app.agent.nodes.result_qa import result_qa_node
from backend.app.agent.schemas import Citation, GroundedAnswer, GroundedClaim, ToolName, ToolResult
from backend.app.agent.verifier import AnswerVerifier


def evidence(rows=None):
    return ToolResult(tool=ToolName.QUERY_RESULT_EVIDENCE, ok=True,
                      artifact="outputs/salt.all.csv", checksum="sha256:fixture",
                      rows=rows if rows is not None else [
                          {"_row_id": 1, "gene_id": "GeneA", "log2FoldChange": "1.2", "padj": "0.001"}],
                      row_count=1, truncated=False, filters={}, sort=None, error_code=None)


def draft(text, row_ids=None, checksum="sha256:fixture"):
    return GroundedAnswer(claims=[GroundedClaim(text=text, citation=Citation(
        artifact="outputs/salt.all.csv", checksum=checksum,
        row_ids=[1] if row_ids is None else row_ids))])


@pytest.mark.parametrize("text,rows,ids", [
    ("GeneZ has log2FoldChange 1.2", None, [1]),
    ("MetB has log2FoldChange 1.2", [{"_row_id": 1, "metabolite_id": "MetA", "log2FoldChange": "1.2"}], [1]),
    ("GeneA and GeneZ have log2FoldChange 1.2", None, [1]),
    ("GeneA has unknown_statistic 1.2", None, [1]),
    ("GeneA has padj 1.2", None, [1]),
    ("GeneA has 1.2", None, [1]),
    ("GeneA is significantly upregulated", None, [1]),
    ("GeneA has log2FoldChange 2.4", [
        {"_row_id": 1, "gene_id": "GeneA", "log2FoldChange": "1.2"},
        {"_row_id": 2, "gene_id": "GeneZ", "log2FoldChange": "2.4"}], [1, 2]),
    ("GeneZ has log2FoldChange 1.2", [], []),
])
def test_entity_field_value_binding_rejects_unsupported_claim(text, rows, ids):
    source = evidence(rows)
    answer = draft(text, ids)
    assert AnswerVerifier().verify(answer, [source]).verdict.value == "rejected"
    assert text not in [c.text for c in GroundedAnswerPipeline().answer(source, answer).claims]


@pytest.mark.parametrize("field,entity,stat,value", [
    ("gene_id", "GeneA", "padj", "0.001"),
    ("metabolite_id", "MetA", "padj_bh", "0.002"),
    ("Gene", "AT1G01010", "PearsonR", "-0.72"),
])
def test_correct_entity_field_value_is_approved(field, entity, stat, value):
    source = evidence([{"_row_id": 1, field: entity, stat: value}])
    answer = draft(f"{entity} has {stat} {value}")
    assert AnswerVerifier().verify(answer, [source]).verdict.value == "approved"
    assert GroundedAnswerPipeline().answer(source, answer) == answer


@pytest.mark.parametrize("ids,checksum", [([99], "sha256:fixture"), ([1], "sha256:wrong")])
def test_bad_citation_falls_back_to_actual_row(ids, checksum):
    source = evidence()
    answer = draft("GeneA has padj 0.001", ids, checksum)
    assert AnswerVerifier().verify(answer, [source]).verdict.value == "rejected"
    safe = GroundedAnswerPipeline().answer(source, answer)
    assert safe.claims[-1].citation.row_ids == [1]
    assert safe.claims[-1].citation.checksum == source.checksum


class ScriptedModel:
    def __init__(self, *outputs):
        self.outputs = list(outputs)

    def __call__(self, context, *, role):
        return self.outputs.pop(0)


def state(**kwargs):
    return GraphState(thread_id="evidence-test", user_id="user-1",
                      user_message=kwargs.pop("user_message", "Show current result padj"),
                      current_job=JobRef(job_id="job-1", owner_id="user-1"), **kwargs)


@pytest.mark.parametrize("action", ["answer", "ask_user"])
def test_result_text_cannot_bypass_query(action):
    text = "GeneZ has padj=0.001"
    output = ({"decision": {"action": action}, "answer": text} if action == "answer"
              else {"decision": {"action": action, "question": text}})
    result = result_qa_agent_node(ScriptedModel(output))(state())
    assert text not in (result["response_text"] or "")
    assert result["grounded_answer"] is None
    assert result["outcome"] == "unresolved"
    assert result["failure_code"] == "result_evidence_required"


def test_plain_answer_after_query_displays_actual_evidence():
    model = ScriptedModel(
        {"decision": {"action": "tool_call", "tool": "query_artifact",
                      "arguments": {"job_id": "job-1", "artifact": "outputs/salt.all.csv"}}},
        {"decision": {"action": "answer"}, "answer": "GeneZ has padj=0.001"})
    source = evidence().model_copy(update={"tool": ToolName.QUERY_ARTIFACT})
    result = result_qa_agent_node(model, lambda *_: source)(state())
    assert "GeneZ" not in result["response_text"]
    assert "gene_id=GeneA" in result["response_text"]
    assert result["grounded_answer"].claims[0].citation.row_ids == [1]


def test_result_reroute_cannot_reopen_free_text_exit():
    model = ScriptedModel(
        {"decision": {"action": "reroute", "reroute_to": "qa"}},
        {"decision": {"action": "answer"}, "answer": "GeneZ has padj=0.001"})
    graph = build_agent_graph(model, lambda *_: [], lambda *_: None,
                              lambda *_: None, lambda *_: None)
    result = graph.invoke(state(), {"configurable": {"thread_id": "reroute"}})
    assert "GeneZ" not in result["response_text"]
    assert result["failure_code"] == "result_evidence_required"


@pytest.mark.parametrize("question", ["What is differential expression?", "What is padj?", "What is a fold change?", "什么是 log2FoldChange？"])
def test_concept_explanation_with_current_job_remains_available(question):
    answer = "This is a general statistical concept."
    result = build_agent_graph(ScriptedModel({"decision": {"action": "answer"}, "answer": answer}),
                               lambda *_: [], lambda *_: None, lambda *_: None, lambda *_: None).invoke(
        state(user_message=question), {"configurable": {"thread_id": question}})
    assert result["response_text"] == answer
    assert result["grounded_answer"] is None


def test_duplicate_evidence_row_ids_are_rejected_by_verifier():
    source = evidence([{"_row_id": 1, "gene_id": "GeneA", "padj": "0.2"},
                       {"_row_id": 1, "gene_id": "GeneZ", "padj": "0.001"}])
    assert AnswerVerifier().verify(draft("GeneZ has padj 0.001"), [source]).verdict.value == "rejected"


def test_result_question_without_job_cannot_use_ordinary_qa():
    result = build_agent_graph(ScriptedModel({"decision": {"action": "answer"},
                                              "answer": "GeneZ has padj=0.001"}),
                               lambda *_: [], lambda *_: None, lambda *_: None, lambda *_: None).invoke(
        GraphState(thread_id="no-job", user_id="user-1", user_message="Show padj result for GeneZ"),
        {"configurable": {"thread_id": "no-job"}})
    assert result["failure_code"] == "result_evidence_required"
    assert "GeneZ" not in result["response_text"]


def test_failed_new_query_does_not_reuse_old_evidence():
    model = ScriptedModel(
        {"decision": {"action": "tool_call", "tool": "query_artifact", "arguments": {"job_id": "job-1", "artifact": "outputs/salt.all.csv"}}},
        {"decision": {"action": "tool_call", "tool": "query_artifact", "arguments": {"job_id": "job-2", "artifact": "outputs/salt.all.csv"}}},
        {"decision": {"action": "answer"}, "answer": "GeneZ has padj 0.001"})
    returns = iter([evidence().model_copy(update={"tool": ToolName.QUERY_ARTIFACT}),
                    evidence([]).model_copy(update={"ok": False, "error_code": "not_found"})])
    result = result_qa_agent_node(model, lambda *_: next(returns))(state())
    assert result["grounded_answer"] is None
    assert result["failure_code"] == "result_evidence_required"


def test_query_result_cannot_substitute_a_different_listed_artifact():
    other = "outputs/other.all.csv"
    source = evidence().model_copy(update={"artifact": other})
    summary = JobSummary(job_id="job-1", owner_id="user-1", status="succeeded",
                         artifacts=["outputs/salt.all.csv", other])
    result = result_qa_node(lambda *_: summary, lambda *_: source)(state(
        decision=AgentDecision(action="query_result", result_query=ResultQuerySpec(artifact="outputs/salt.all.csv"))))
    assert result["grounded_answer"] is None
    assert "GeneA" not in result["response_text"]


def test_failed_draft_displays_query_conditions_with_raw_rows():
    source = evidence().model_copy(update={"tool": ToolName.QUERY_ARTIFACT, "filters": {"gene_id": "GeneA"}, "sort": "padj asc"})
    model = ScriptedModel(
        {"decision": {"action": "tool_call", "tool": "query_artifact", "arguments": {"job_id": "job-1", "artifact": "outputs/salt.all.csv"}}},
        {"decision": {"action": "grounded_answer", "grounded_answer": draft("GeneZ has padj 0.001").model_dump()}})
    result = result_qa_agent_node(model, lambda *_: source)(state())
    assert '"gene_id": "GeneA"' in result["response_text"]
    assert "padj asc" in result["response_text"]
    assert "gene_id=GeneA" in result["response_text"]
    assert "GeneZ" not in result["response_text"]


@pytest.mark.parametrize("question", ["Which gene is most significant?", "哪些代谢物上调？", "GeneA log2FoldChange?", "GeneA PearsonR?", "MetA vip?"])
def test_entity_result_questions_cannot_fall_through_to_general_qa(question):
    result = build_agent_graph(ScriptedModel({"decision": {"action": "answer"}, "answer": "GeneZ has padj 0.001"}),
                               lambda *_: [], lambda *_: None, lambda *_: None, lambda *_: None).invoke(
        state(user_message=question), {"configurable": {"thread_id": "entity-query"}})
    assert result["failure_code"] == "result_evidence_required"


def test_pending_analysis_does_not_turn_result_explanation_into_concept_qa():
    pending = PendingAnalysisClarification(question="Which contrast?", source_message="Run DEG")
    result = build_agent_graph(ScriptedModel({"decision": {"action": "answer"}, "answer": "GeneZ has padj 0.001"}),
                               lambda *_: [], lambda *_: None, lambda *_: None, lambda *_: None).invoke(
        state(user_message="Explain the current result for GeneZ", pending_analysis=pending),
        {"configurable": {"thread_id": "pending-result"}})
    assert result["failure_code"] == "result_evidence_required"


def test_result_followup_requires_fresh_query_instead_of_replaying_history():
    recent = RecentMessages(context_version="test", messages=[RecentMessage(
        role="assistant", turn_id="previous", text="gene_id=GeneA; padj=0.001")])
    result = build_agent_graph(ScriptedModel({"decision": {"action": "answer"}, "answer": "GeneZ has padj 0.001"}),
                               lambda *_: [], lambda *_: None, lambda *_: None, lambda *_: None).invoke(
        state(user_message="再解释一下", recent_messages=recent), {"configurable": {"thread_id": "followup"}})
    assert result["failure_code"] == "result_evidence_required"
    assert "GeneZ" not in result["response_text"]


def test_model_citation_alone_is_not_query_evidence():
    result = result_qa_agent_node(ScriptedModel({"decision": {"action": "grounded_answer",
        "grounded_answer": draft("GeneZ has padj 0.001").model_dump()}}))(state())
    assert result["grounded_answer"] is None
    assert result["failure_code"] == "result_evidence_required"
