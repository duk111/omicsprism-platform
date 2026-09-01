from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.agent.eval_v2 import (
    AgentEvalV2Report,
    run_ci_agent_evaluation,
    run_live_model_agent_evaluation,
)


def _print_report(report: AgentEvalV2Report) -> None:
    quality = report.quality
    print(f"Runner: {report.runner}")
    print(f"Trials per case: {report.trials_per_case}")
    print(
        "Agent quality: "
        f"{quality.case_count} cases, pass@1={quality.pass_at_1:.3f}, "
        f"consistency={quality.multi_trial_consistency:.3f}"
    )
    print(
        "Safety and grounding: "
        f"illegal_auto_execution={quality.illegal_auto_execution_count}, "
        f"citation_validity={quality.citation_validity:.3f}, "
        f"numeric_consistency={quality.numeric_consistency:.3f}, "
        f"unsupported_claim_rate={quality.unsupported_claim_rate:.3f}"
    )
    print(
        "Trace and usage: "
        f"linked_trials={quality.trace_linked_trials}, "
        f"reported_tokens={quality.reported_total_tokens}, "
        f"unknown_usage_calls={quality.unknown_usage_model_calls}, "
        f"mean_latency_ms={quality.mean_latency_ms:.3f}, "
        f"p95_latency_ms={quality.p95_latency_ms:.3f}"
    )
    capability = report.capability
    print(
        "Capability isolation (reported separately): "
        f"cases={capability.case_count}, pass@1={capability.pass_at_1:.3f}, "
        f"tool_parameter_accuracy={capability.tool_parameter_accuracy:.3f}, "
        f"illegal_auto_execution={capability.illegal_auto_execution_count}"
    )
    print(f"Evaluator self-test: {'PASS' if report.evaluator_self_test_passed else 'FAIL'}")
    for case in report.cases:
        if case.status != "passed":
            print(f"  {case.status}: {case.case_id}")
    print(f"Release gate: {'PASS' if report.release_gate.passed else 'FAIL'}")
    for failure in report.release_gate.failures:
        print(f"  - {failure}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run OmicsPrism Eval v2 through isolated Agent graph scenarios."
    )
    parser.add_argument("--json", action="store_true", help="print the typed report as JSON")
    parser.add_argument(
        "--trials", type=int, help="trials per case for CI or live evaluation (1-10)"
    )
    parser.add_argument(
        "--live-model",
        action="store_true",
        help="call an explicitly configured OpenAI-compatible model; never enabled by CI defaults",
    )
    parser.add_argument("--base-url", help="OpenAI-compatible API base URL, required with --live-model")
    parser.add_argument("--model", help="served model name, required with --live-model")
    parser.add_argument("--api-key", help="optional model API key; do not place it in shell history")
    args = parser.parse_args(argv)
    trials = args.trials if args.trials is not None else (3 if args.live_model else 1)
    if not 1 <= trials <= 10:
        parser.error("--trials must be between 1 and 10")
    if args.live_model:
        if not args.base_url or not args.model:
            parser.error("--live-model requires both --base-url and --model")
        report = run_live_model_agent_evaluation(
            base_url=args.base_url,
            model_name=args.model,
            api_key=args.api_key,
            trials_per_case=trials,
        )
    else:
        report = run_ci_agent_evaluation(trials_per_case=trials)
    if args.json:
        print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
    else:
        _print_report(report)
    return 0 if report.release_gate.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
