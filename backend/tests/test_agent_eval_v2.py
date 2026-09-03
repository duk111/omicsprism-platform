from __future__ import annotations

import json

import pytest

from backend.app.agent.eval_v2 import (
    AgentEvalV2Case,
    AgentPricingTable,
    AgentModelPricing,
    DEFAULT_EVAL_V2_CASES_PATH,
    EvalGateConfig,
    EvalExpectation,
    compare_agent_eval_reports,
    evaluate_report_release_gate,
    load_agent_eval_v2_cases,
    run_ci_agent_evaluation,
)
from backend.app.agent.graph import MainModelOutput
from scripts.run_agent_eval_v2 import main


def _quality_cases() -> list[AgentEvalV2Case]:
    return [case for case in load_agent_eval_v2_cases() if case.suite == "agent_quality"]


def test_fixture_schema_ids_and_required_phase_two_coverage() -> None:
    cases = load_agent_eval_v2_cases(DEFAULT_EVAL_V2_CASES_PATH)
    quality = [case for case in cases if case.suite == "agent_quality"]
    counts = {
        category: sum(case.category == category for case in quality)
        for category in {
            "multi_turn_memory",
            "ambiguity",
            "confirmation",
            "result_grounding",
            "capability_isolation",
        }
    }

    assert len(cases) == 50
    assert len(quality) == 47
    assert len({case.case_id for case in cases}) == len(cases)
    assert counts == {
        "multi_turn_memory": 8,
        "ambiguity": 23,
        "confirmation": 4,
        "result_grounding": 6,
        "capability_isolation": 6,
    }
    assert sum(case.suite == "evaluator_self_test" for case in cases) == 3


def test_case_schema_rejects_duplicate_transport_data_and_invalid_non_gating_case() -> None:
    payload = _quality_cases()[0].model_dump(mode="json")
    payload["turns"][0]["unexpected"] = True
    with pytest.raises(ValueError, match="unexpected"):
        AgentEvalV2Case.model_validate(payload)

    with pytest.raises(ValueError, match="non-gating"):
        EvalExpectation(terminal="completed", expected_job_count=0, release_gate=False)


def test_ci_recordings_are_reproducible_and_trials_are_environment_isolated() -> None:
    cases = [
        next(case for case in _quality_cases() if case.case_id == "confirmation-approve-001"),
        next(case for case in _quality_cases() if case.case_id == "result-query-gene-001"),
    ]
    self_tests = [
        case
        for case in load_agent_eval_v2_cases()
        if case.suite == "evaluator_self_test"
    ]
    first = run_ci_agent_evaluation(cases=[*cases, *self_tests], trials_per_case=2)
    second = run_ci_agent_evaluation(cases=[*cases, *self_tests], trials_per_case=2)

    assert first.release_gate.passed
    assert second.release_gate.passed
    assert [
        (case.case_id, [trial.model_dump(exclude={"latency_ms"}) for trial in case.trials])
        for case in first.cases
    ] == [
        (case.case_id, [trial.model_dump(exclude={"latency_ms"}) for trial in case.trials])
        for case in second.cases
    ]
    quality_cases = [case for case in first.cases if case.suite == "agent_quality"]
    assert all(case.consistency == 1 for case in first.cases)
    assert all(case.trials[0].trace_event_count > 0 for case in quality_cases)
    assert all(case.trials[0].reported_total_tokens is not None for case in quality_cases)


def test_eval_multiturn_live_adapter_receives_recent_user_and_assistant_messages() -> None:
    case = next(
        case for case in _quality_cases() if case.case_id == "memory-correction-001"
    )
    class SpyModel:
        last_usage = None

        def __init__(self) -> None:
            self.contexts = []

        def __call__(self, context):
            self.contexts.append(context)
            return MainModelOutput.model_validate({
                "decision": {"action": "answer"},
                "answer": "A concise answer.",
            })

    model: SpyModel | None = None

    def factory(_recorder):
        nonlocal model
        model = SpyModel()
        return model

    from backend.app.agent import eval_v2

    result = eval_v2._run_graph_trial(case, 1, factory)
    assert result.matched
    assert model is not None
    assert len(model.contexts) == 2
    assert [(item.role, item.text) for item in model.contexts[1].recent_messages.messages] == [
        ("user", "I am exploring differential expression."),
        ("assistant", "A concise answer."),
    ]


def test_confirmation_uses_checkpoint_plan_values_and_detects_fingerprint_change() -> None:
    cases = _quality_cases()
    approve = next(case for case in cases if case.case_id == "confirmation-approve-001")
    changed = next(case for case in cases if case.case_id == "confirmation-fingerprint-change-004")
    report = run_ci_agent_evaluation(cases=[approve, changed])
    approve_trial, changed_trial = (case.trials[0] for case in report.cases)

    assert approve_trial.matched and approve_trial.job_count == 1
    assert changed_trial.matched
    assert changed_trial.terminal == "interrupt"
    assert changed_trial.interrupt_kind == "clarification"
    assert changed_trial.job_count == 0


def test_evaluator_self_tests_keep_malformed_evidence_out_of_agent_quality_metrics() -> None:
    report = run_ci_agent_evaluation()

    assert report.evaluator_self_test_passed
    assert report.release_gate.passed
    assert report.quality.case_count == 41
    assert report.quality.release_case_count == 41
    assert report.quality.known_gap_count == 0
    assert report.capability.case_count == 6
    assert report.capability.pass_at_1 == 1
    assert report.capability.tool_parameter_accuracy == 1
    assert report.capability.illegal_auto_execution_count == 0
    assert report.quality.citation_validity == 1
    assert report.quality.numeric_consistency == 1
    assert report.quality.unsupported_claim_rate == 0
    assert all(
        case.status == "passed"
        for case in report.cases
        if case.suite == "evaluator_self_test"
    )


def test_release_gate_fails_when_a_gated_grounded_case_regresses() -> None:
    case = next(case for case in _quality_cases() if case.case_id == "result-query-gene-001")
    broken = case.model_copy(update={
        "expected": case.expected.model_copy(update={"expects_grounded_answer": False}),
    })
    report = run_ci_agent_evaluation(cases=[broken])

    assert not report.release_gate.passed
    assert "release scenario terminal success" in report.release_gate.failures[0]


def test_cli_json_defaults_to_recorded_ci_runner_without_external_model(capsys) -> None:
    assert main(["--json", "--trials", "2"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["runner"] == "ci"
    assert payload["trials_per_case"] == 2
    assert payload["release_gate"]["passed"] is True
    assert payload["quality"]["unknown_usage_model_calls"] == 0


def test_live_runner_requires_explicit_endpoint_and_model() -> None:
    with pytest.raises(SystemExit):
        main(["--live-model"])


def test_report_contains_versions_usage_latency_and_unknown_local_cost() -> None:
    report = run_ci_agent_evaluation(trials_per_case=1)

    assert report.graph_version == "agent-graph.v3"
    assert report.prompt_version == "main-system.v1"
    assert report.model_provider == "recorded-fixture"
    assert report.model_name == "recorded-ci-model"
    assert report.model_call_count > 0
    assert report.tool_call_count >= 0
    assert report.latency.p50_turn_ms >= 0
    assert report.latency.p95_turn_ms >= report.latency.p50_turn_ms
    assert report.latency.ttft_definition == "first_visible_event"
    assert report.cost.status == "unknown"
    assert report.cost.prompt_tokens > 0
    assert report.cost.completion_tokens > 0
    assert report.cost.total_cost_usd is None
    diff = compare_agent_eval_reports(report, report.model_copy(deep=True))
    assert diff.token_delta == 0
    assert diff.cost_status == "unknown"


def test_configured_price_card_calculates_cost_and_diff_reports_versions() -> None:
    pricing = AgentPricingTable(entries=[AgentModelPricing(
        provider="recorded-fixture",
        model="recorded-ci-model",
        input_usd_per_million=1,
        output_usd_per_million=2,
        source="test-price-card.v1",
    )])
    baseline = run_ci_agent_evaluation(cases=[_quality_cases()[0]], pricing=pricing)
    candidate = baseline.model_copy(deep=True, update={
        "graph_version": "agent-graph.v4",
        "run_id": "candidate-run",
    })

    assert baseline.cost.status == "calculated"
    assert baseline.cost.total_cost_usd is not None
    assert baseline.cost.total_cost_usd > 0
    diff = compare_agent_eval_reports(baseline, candidate)
    assert diff.versions_changed is True
    assert diff.cost_status == "calculated"
    assert diff.cost_delta_usd == 0
    assert diff.token_delta == 0


def test_release_gate_uses_configured_memory_and_unsupported_claim_thresholds() -> None:
    report = run_ci_agent_evaluation(trials_per_case=1)
    degraded = report.model_copy(update={
        "quality": report.quality.model_copy(update={
            "multi_turn_memory_accuracy": 0.75,
            "unsupported_claim_rate": 0.1,
        }),
    })
    gate = evaluate_report_release_gate(
        degraded,
        EvalGateConfig(
            min_multi_turn_memory_accuracy=0.875,
            max_unsupported_claim_rate=0.05,
            max_p95_turn_ms=None,
            max_p95_model_ms=None,
            max_p95_tool_ms=None,
        ),
    )

    assert not gate.passed
    assert any("multi-turn memory" in failure for failure in gate.failures)
    assert any("unsupported claim" in failure for failure in gate.failures)


def test_release_gate_enforces_latency_budget_and_cross_user_zero() -> None:
    report = run_ci_agent_evaluation(trials_per_case=1)
    degraded = report.model_copy(update={
        "latency": report.latency.model_copy(update={"p95_turn_ms": 1001}),
        "capability": report.capability.model_copy(update={"cross_user_access_count": 1}),
    })
    gate = evaluate_report_release_gate(
        degraded,
        EvalGateConfig(
            max_p95_turn_ms=1000,
            max_p95_model_ms=None,
            max_p95_tool_ms=None,
        ),
    )

    assert not gate.passed
    assert "cross-user access is non-zero" in gate.failures
    assert any("p95 turn latency" in failure for failure in gate.failures)


def test_unknown_cost_is_allowed_or_rejected_only_by_explicit_policy() -> None:
    report = run_ci_agent_evaluation(trials_per_case=1)
    allowed = evaluate_report_release_gate(
        report,
        EvalGateConfig(
            max_cost_usd=0,
            max_p95_turn_ms=None,
            max_p95_model_ms=None,
            max_p95_tool_ms=None,
            require_cost_known=False,
        ),
    )
    required = evaluate_report_release_gate(
        report,
        EvalGateConfig(
            max_p95_turn_ms=None,
            max_p95_model_ms=None,
            max_p95_tool_ms=None,
            require_cost_known=True,
        ),
    )

    assert allowed.passed
    assert not required.passed
    assert "cost is unknown but a known cost is required" in required.failures


def test_compare_reports_surfaces_gate_and_budget_regressions() -> None:
    baseline = run_ci_agent_evaluation(trials_per_case=1)
    candidate = baseline.model_copy(update={
        "run_id": "candidate-run",
        "latency": baseline.latency.model_copy(update={"p95_turn_ms": 1001}),
    })
    diff = compare_agent_eval_reports(baseline, candidate)

    assert diff.release_gate_changed is True
    assert diff.budget_regression is True
    assert any("p95 turn latency" in failure for failure in diff.new_gate_failures)
