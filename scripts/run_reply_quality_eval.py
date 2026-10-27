"""Read-only live reply audit; production graph/tools, retained Worker artifacts.

No scientific jobs or production stores are used. Raw model messages are retained
alongside final blocks so model, context, rendering and verifier issues stay distinct.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.agent.capabilities import CapabilityPrincipal, build_readonly_capability_registry
from backend.app.agent.graph import GraphState, JobRef, build_agent_graph
from backend.app.agent.model import VllmGraphModel, _ROLE_CONFIG
from backend.app.agent.nodes.result_qa import job_reader_from_runtime, result_querier_from_runtime
from backend.app.agent.verifier import AnswerVerifier
from backend.app.models import AnalysisType
from backend.tests.test_agent_artifact_e2e import persisted_runtime

CASES = [
    ("q01", "qa", "什么是火山图？请简短解释横轴和纵轴。"),
    ("q02", "qa", "DEG 中 padj 是什么？padj 小是否意味着变化幅度大？"),
    ("q03", "qa", "What does an adjusted p-value mean, and does it measure effect size?"),
    ("q04", "qa", "GMA 中相关性很高，能说明基因调控了代谢物吗？"),
    ("q05", "qa", "log2FoldChange 的正负如何解释？需要知道什么比较方向？"),
    ("q06", "qa", "代谢组分析中 VIP 和 padj 分别说明什么？能只看 VIP 吗？"),
    ("a01", "analysis", "我只有基因 counts 和样本分组表，可以做什么分析？请简短说明。"),
    ("a02", "analysis", "What inputs do I need to configure DEG? Do not start an analysis."),
    ("a03", "analysis", "帮我配置 DEM 分析，需要我提供哪些文件？先不要运行。"),
    ("a04", "analysis", "配置 DEG 时，应该怎样指定 tested 和 reference？不要猜我的实验分组。"),
    ("r01", "deg", "当前 DEG 结果中 padj 最小的基因是什么？只给出基因和 padj。"),
    ("r02", "deg", "Which gene has the smallest padj in the current DEG results? Give its name and padj only."),
    ("r03", "deg", "查询 gene_up 的 log2FoldChange 和 padj，简短解释它们各代表什么。"),
    ("r04", "deg", "当前 DEG 结果中 padj 小于 0.05 的基因有哪些？列出 gene_id 和 padj。"),
    ("r05", "deg", "查询 NotARealGene 的 padj。如果查不到请直接说明。"),
    ("r06", "dem", "当前 DEM 结果中 padj_bh 最小的代谢物是什么？给出名称和 padj_bh。"),
    ("r07", "dem", "Show the log2FoldChange and padj_bh for metab_high in the current DEM results."),
    ("r08", "gma", "当前 GMA 结果中 gene_0 和 metab_0 的 PearsonR 是多少？这能证明因果关系吗？"),
    ("r09", "gma", "Show the PearsonR and EdgeWeight for gene_0 and metab_0 in the current GMA results."),
    ("r10", "no_job", "查询我当前任务中 gene_up 的 padj。"),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--only", help="comma-separated case IDs for diagnosis")
    args = parser.parse_args()
    output = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "base_url": args.base_url, "model": args.model,
        "settings": {"temperature": 0, "max_tokens": 768, "enable_thinking": False,
                     "structured_tool_response": False},
        "scope": "Live deployed model; isolated production LangGraph, disk artifacts, registry and verifier. No deployed API/UI, queue or scientific execution.",
        "prompt_hashes": {role.value: hashlib.sha256(config["system_prompt"].encode()).hexdigest()
                          for role, config in _ROLE_CONFIG.items()},
        "cases": [],
    }
    with httpx.Client(timeout=90, trust_env=False) as discovery:
        response = discovery.get(args.base_url.rstrip("/") + "/models")
        response.raise_for_status()
        report["model_discovery"] = response.json()
    for cid, mode, question in CASES:
        if args.only and cid not in args.only.split(","):
            continue
        record = {"id": cid, "mode": mode, "question": question,
                  "model_calls": [], "tools": []}
        with TemporaryDirectory(prefix="omicsprism-reply-audit-") as temp:
            kind = AnalysisType(mode) if mode in {"deg", "dem", "gma"} else AnalysisType.DEG
            filename = "T02_High_Confidence_Network.csv" if kind is AnalysisType.GMA else kind.value + ".all.csv"
            content = (ROOT / "backend/app/agent/fixtures/result_query" / filename).read_bytes()
            runtime, job = persisted_runtime(Path(temp), kind, content, filename)
            registry = build_readonly_capability_registry(runtime)
            evidence = []

            def capture_response(response):
                response.read()
                try:
                    payload = response.json()
                except ValueError:
                    payload = {"error": response.text[:1000]}
                record["model_calls"].append({"status": response.status_code, "payload": payload})

            def execute(request, state):
                result = registry.invoke(request.tool.value, request.arguments,
                                         principal=CapabilityPrincipal(subject=state.user_id, transport="internal"))
                record["tools"].append({"tool": request.tool.value, "arguments": request.arguments,
                                        "result": result.model_dump(mode="json")})
                if request.tool.value == "query_artifact" and result.ok:
                    evidence.append(result)
                return result

            def query(request):
                result = result_querier_from_runtime(runtime)(request)
                record["tools"].append({"tool": "query_result", "arguments": request.query.model_dump(mode="json"),
                                        "result": result.model_dump(mode="json")})
                evidence.append(result)
                return result

            def no_execution(*_args, **_kwargs):
                raise AssertionError("Reply quality audit must not submit an analysis")

            with httpx.Client(timeout=90, trust_env=False, event_hooks={"response": [capture_response]}) as client:
                model = VllmGraphModel(base_url=args.base_url, model=args.model, client=client,
                                       structured_tool_response=False)
                graph = build_agent_graph(model, lambda *_: [], no_execution,
                                          job_reader_from_runtime(runtime), query, tool_executor=execute)
                state = GraphState(thread_id="reply-" + cid, user_id="user-1", user_message=question,
                                   current_job=JobRef(job_id=job.id, owner_id="user-1") if mode in {"deg", "dem", "gma"} else None)
                started = perf_counter()
                try:
                    result = graph.invoke(state, {"configurable": {"thread_id": state.thread_id}})
                    final = GraphState.model_validate(result)
                    record.update(answer=final.response_text,
                                  blocks=[block.model_dump(mode="json") for block in final.response_blocks],
                                  outcome=final.outcome, failure_code=final.failure_code,
                                  decision=final.decision.model_dump(mode="json") if final.decision else None,
                                  budget=final.step_budget.model_dump(mode="json"),
                                  routes=[role.value for role in final.visited_roles])
                    if final.grounded_answer is not None and evidence:
                        record["verification"] = AnswerVerifier().verify(final.grounded_answer, [evidence[-1]]).model_dump(mode="json")
                except Exception as exc:
                    record["error"] = {"type": type(exc).__name__, "message": str(exc)[:2000]}
                record["latency_ms"] = round((perf_counter() - started) * 1000, 2)
                record["contexts"] = [context.model_dump(mode="json") for context in model.contexts]
        report["cases"].append(record)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"id": cid, "calls": len(record["model_calls"]), "ms": record["latency_ms"],
                          "answer": record.get("answer"), "error": record.get("error")}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
