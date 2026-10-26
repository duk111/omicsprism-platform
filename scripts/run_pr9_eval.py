"""Run PR9 scenarios through AgentEvalV2 (scripted responses; no live model)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.agent.eval_v2 import AgentEvalV2Case, load_agent_eval_v2_cases, run_ci_agent_evaluation


def cases() -> list[AgentEvalV2Case]:
    rows = [
        {"_row_id": index, "gene_id": f"Gene{letter}", "log2FoldChange": str(fc), "padj": str(index / 1000)}
        for index, (letter, fc) in enumerate(zip("ABCDEZ", [1, 5, 3, 2, 4, 99]), 1)
    ]
    job = {"job_id": "job-a", "artifacts": ["outputs/salt.all.csv"], "checksum": "sha256:pr9-fixture", "evidence_rows": rows}
    job_b = {**job, "job_id": "job-b", "checksum": "sha256:pr9-job-b", "evidence_rows": [
        {"_row_id": 1, "gene_id": "PrivateB", "log2FoldChange": "7", "padj": "0.02"}]}
    response = {"decision": {"action": "answer"}, "answer": "A general explanation."}
    result = []
    def add(cid, messages, routes, *, env=None, responses=None, expected=None, category="ambiguity"):
        payload = {
            "case_id": "pr9-" + cid, "suite": "agent_quality", "category": category,
            "environment": env or {"jobs": [job], "focus_job_ids": ["job-a"]},
            "turns": [{"kind": "message", "message": message, "recorded_responses": outputs}
                      for message, outputs in zip(messages, responses or [[response] for _ in messages])],
            "tool_policy": {"allowed_tools": ["query_artifact"]},
            "expected": {"terminal": "completed", "expected_job_count": 0, "expected_routes": routes, **(expected or {})},
            "grader": {"deterministic": "graph_terminal"},
        }
        result.append(AgentEvalV2Case.model_validate(payload))

    for cid, message in [
        ("volcano-qa", "什么是火山图？"), ("metric-qa", "DEG 中 padj 表示什么？"),
        ("metric-qa-en", "What does padj mean in DEG?"),
    ]:
        add(cid, [message], ["qa"], expected={"expected_decision": "answer"})
    for cid, message in [
        ("volcano-result", "当前火山图有哪些显著基因？"),
        ("gma-result", "当前 GMA 结果中 GeneA 关联了哪些代谢物？"),
    ]:
        add(cid, [message], ["result_qa"], expected={"expected_decision": "ask_user", "expects_grounded_answer": False})
    add("configure", ["帮我配置 DEG 分析。"], ["analysis"], env={}, responses=[[
        {"decision": {"action": "ask_user", "question": "Please select the analysis input files."}}]],
        expected={"terminal": "interrupt", "interrupt_kind": "clarification"})
    add("run-with-metric", ["Run DEG with padj cutoff 0.01"], ["analysis"], responses=[[
        {"decision": {"action": "ask_user", "question": "Please select the analysis input files."}}]],
        expected={"terminal": "interrupt", "interrupt_kind": "clarification"})
    env = {"jobs": [job, job_b], "focus_job_ids": ["job-a", "job-b"]}
    add("ambiguous", ["解释一下这个。"], ["ambiguous"], env=env,
        responses=[[]], expected={"expected_decision": "ask_user", "response_contains": ["Job"]})
    add("no-job", ["Show padj result for GeneA"], ["result_qa"], env={"jobs": []},
        expected={"expected_decision": "ask_user", "expects_grounded_answer": False})

    def query(filters=None):
        return {"decision": {"action": "query_result", "job_id": "job-a", "result_query": {
            "artifact": "outputs/salt.all.csv", "sort": "padj asc", "limit": 5 if not filters else 1,
            "filters": filters or {},
        }}}
    multi_env = {"jobs": [job], "focus_job_ids": ["job-a"], "inherit_result_context": True}
    add("memory-gene", ["查询 GeneA 的差异分析结果。", "它的 padj 是多少？"], ["result_qa", "result_qa"],
        env=multi_env, responses=[[query({"gene_id": "GeneA"})], []], category="multi_turn_memory",
        expected={"expected_row_ids": [1], "response_contains": ["GeneA", "0.001"], "response_excludes": ["GeneZ"], "require_grounded_answer": True})
    add("memory-set", ["当前 DEG 结果中 padj 最小的 5 个基因是什么？", "其中哪个 log2FoldChange 最大？"], ["result_qa", "result_qa"],
        env=multi_env, responses=[[query()], []], category="multi_turn_memory",
        expected={"expected_row_ids": [2], "response_contains": ["GeneB", "5"], "response_excludes": ["GeneZ"], "require_grounded_answer": True})
    add("switch", ["查询 GeneA 的差异分析结果。", "它的 padj 是多少？"], ["result_qa", "result_qa"],
        env={**multi_env, "jobs": [job, job_b]}, responses=[[query({"gene_id": "GeneA"})], []],
        category="multi_turn_memory", expected={"expected_final_job_id": "job-b", "expects_grounded_answer": False, "response_excludes": ["GeneA", "0.001"]})
    result[-1].turns[1].focus_job_ids = ["job-b"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    scenarios = cases()
    self_tests = [case for case in load_agent_eval_v2_cases() if case.suite == "evaluator_self_test"]
    report = run_ci_agent_evaluation(cases=[*scenarios, *self_tests])
    trials = [case.trials[0] for case in report.cases if case.suite == "agent_quality"]
    summary = {
        "runner": "AgentEvalV2 recorded/scripted model, not live model quality",
        "case_pass_rate": report.quality.pass_at_1,
        "intent_routing_accuracy": sum(t.route_targets == case.expected.expected_routes for t, case in zip(trials, scenarios)) / len(scenarios),
        "result_qa_routing_accuracy": sum(t.route_targets == case.expected.expected_routes for t, case in zip(trials, scenarios) if "result_qa" in case.expected.expected_routes) / sum("result_qa" in case.expected.expected_routes for case in scenarios),
        "context_inheritance_accuracy": sum(t.matched for t in trials if "memory-" in t.case_id) / 2,
        "job_switch_isolation_accuracy": float(next(t.matched for t in trials if t.case_id == "pr9-switch")),
        "reroute_count": sum(t.reroute_count for t in trials),
        "model_calls": sum(t.model_call_count for t in trials),
        "mean_model_calls_per_turn": sum(t.model_call_count for t in trials) / sum(len(c.turns) for c in scenarios),
        "latency": report.latency.model_dump(mode="json"),
    }
    args.output.write_text(json.dumps({"summary": summary, "report": report.model_dump(mode="json")}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    for case in report.cases:
        if case.status != "passed":
            print("FAILED", case.case_id, case.trials[0].failure_code, case.trials[0].route_targets)
    return 0 if all(trial.matched for trial in trials) else 1


if __name__ == "__main__":
    raise SystemExit(main())
