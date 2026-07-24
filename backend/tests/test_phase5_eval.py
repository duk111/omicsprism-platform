from __future__ import annotations

from collections import Counter
import json

import httpx

from backend.app.agent.eval import (
    DEFAULT_CASES_PATH,
    EvalAssembly,
    EvalAssemblyFactory,
    EvalRunner,
    compare_reports,
    load_golden_cases,
)
from backend.app.agent.model import VllmModelAdapter
from backend.app.agent.schemas import (
    ActiveProfile,
    AgentState,
    EvalAssemblyName,
    EvalCaseStatus,
    EvalCategory,
    ModelContext,
    ToolName,
)


def test_golden_suite_has_required_size_distribution_and_adversarial_cases() -> None:
    cases = load_golden_cases(DEFAULT_CASES_PATH)

    assert len(cases) == 25
    assert len({case.case_id for case in cases}) == 25
    assert Counter(case.category for case in cases) == {
        EvalCategory.ROUTER: 5,
        EvalCategory.RECOMMENDATION: 4,
        EvalCategory.CONTRAST: 4,
        EvalCategory.FAILURE: 3,
        EvalCategory.GROUNDING: 9,
    }
    adversarial_ids = {case.case_id for case in cases if case.adversarial}
    assert {
        "ground_union_direction_001",
        "ground_edgeweight_not_correlation_001",
        "ground_opls_score_not_vip_001",
        "ground_empty_padj_001",
        "ground_cross_user_404_001",
        "ground_causal_claim_001",
        "ground_cell_injection_001",
    }.issubset(adversarial_ids)


def test_unit_fixture_assembly_replays_all_cases_without_external_services() -> None:
    cases = load_golden_cases(DEFAULT_CASES_PATH)
    report = EvalRunner().run(cases, EvalAssemblyFactory.unit())

    assert report.assembly is EvalAssemblyName.UNIT
    assert report.summary.total == 25
    assert report.summary.passed == 25
    assert report.summary.failed == 0
    assert report.summary.skipped == 0
    assert report.summary.model_calls == 4
    assert report.summary.metrics["schema_validity"] == 1.0
    assert report.summary.metrics["route_accuracy"] == 1.0
    assert report.summary.metrics["recommendation_accuracy"] == 1.0
    assert report.summary.metrics["contrast_block_rate"] == 1.0
    assert report.summary.metrics["unapproved_job_creations"] == 0.0
    assert report.summary.metrics["cross_user_access_successes"] == 0.0
    assert report.summary.metrics["numeric_accuracy"] == 1.0
    assert report.summary.metrics["citation_coverage"] == 1.0


def test_unit_recommendation_is_driven_by_inputs_not_expected_answer() -> None:
    case = next(case for case in load_golden_cases(DEFAULT_CASES_PATH) if case.case_id == "recommend_deg_001")
    case.expected["analysis_recommendations"] = ["correlation"]

    report = EvalRunner().run([case], EvalAssemblyFactory.unit())

    assert report.summary.failed == 1
    assert report.case_results[0].schema_valid is True


def test_schema_validity_records_invalid_live_model_output() -> None:
    case = next(case for case in load_golden_cases(DEFAULT_CASES_PATH) if case.case_id == "recommend_deg_001")

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    assembly = EvalAssembly(
        name=EvalAssemblyName.OFFLINE,
        label="invalid-model",
        available=True,
        skip_reason=None,
        model=VllmModelAdapter(
            base_url="http://model-host:8000",
            model="invalid-model",
            client=httpx.Client(transport=httpx.MockTransport(handle)),
        ),
        fixture_tools=True,
        in_memory_state=True,
    )

    report = EvalRunner().run([case], assembly)

    assert report.summary.failed == 1
    assert report.case_results[0].schema_valid is False
    assert report.summary.metrics["schema_validity"] == 0.0


def test_live_assemblies_explicitly_skip_when_required_configuration_is_missing() -> None:
    cases = load_golden_cases(DEFAULT_CASES_PATH)
    factory = EvalAssemblyFactory(env={})

    offline = EvalRunner().run(cases, factory.offline())
    production = EvalRunner().run(cases, factory.production())

    assert offline.assembly is EvalAssemblyName.OFFLINE
    assert offline.summary.skipped == 25
    assert "OMICS_PRISM_AGENT_MODEL_URL" in (offline.skip_reason or "")
    assert production.assembly is EvalAssemblyName.PRODUCTION
    assert production.summary.skipped == 25
    assert "OMICS_PRISM_RUNTIME_DATABASE_URL" in (production.skip_reason or "")
    assert offline.summary.metrics["schema_validity"] is None
    assert offline.summary.metrics["unapproved_job_creations"] is None
    assert production.summary.metrics["cross_user_access_successes"] is None


def test_live_assembly_configuration_is_explicit_about_backends() -> None:
    factory = EvalAssemblyFactory(env={
        "OMICS_PRISM_AGENT_MODEL_URL": "http://model-host:8000",
        "OMICS_PRISM_AGENT_MODEL_NAME": "Qwen3-14B-AWQ",
        "OMICS_PRISM_RUNTIME_DATABASE_URL": "postgresql://omics_app:redacted@db/omics",
        "OMICS_PRISM_EVAL_CROSS_USER_JOB_ID": "job-owned-by-another-user",
        "OMICS_PRISM_EVAL_CROSS_USER_ID": "attacker-user",
    })

    offline = factory.offline()
    production = factory.production()

    assert offline.available and offline.fixture_tools and offline.in_memory_state
    assert production.available and not production.fixture_tools and not production.in_memory_state
    assert offline.model is not None and production.model is not None


def test_vllm_adapter_sends_only_minimal_context_and_validates_response() -> None:
    captured = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        decision = {
            "action": "propose_plan",
            "reasoning_summary": "inputs support DEG",
            "feasibility": {
                "verdict": "answerable",
                "reasons": ["counts and metadata are present"],
                "missing_information": [],
            },
            "analysis_recommendations": ["differential"],
            "requires_approval": True,
            "requested_params": {},
        }
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(decision)}}],
        })

    adapter = VllmModelAdapter(
        base_url="http://model-host:8000",
        model="Qwen3-14B-AWQ",
        client=httpx.Client(transport=httpx.MockTransport(handle)),
    )
    context = ModelContext(
        user_message="recommend an analysis",
        active_profile=ActiveProfile.ANALYSIS,
        state=AgentState.CHECK_INPUTS,
        in_scope_job_ids=[],
        conversation_summary="Available inputs: counts, metadata",
        available_tools=[ToolName.GET_ANALYSIS_SPEC],
    )

    result = adapter.decide(context)

    sent_context = json.loads(captured["body"]["messages"][1]["content"])
    assert captured["url"] == "http://model-host:8000/v1/chat/completions"
    assert set(sent_context) == set(context.model_dump(mode="json"))
    assert "database_url" not in sent_context
    assert captured["body"]["response_format"]["type"] == "json_schema"
    assert captured["body"]["response_format"]["json_schema"]["strict"] is True
    assert captured["body"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert captured["body"]["max_tokens"] == 512
    assert result.analysis_recommendations[0].value == "differential"


def test_live_recommendation_receives_structured_registry_requirements() -> None:
    captured = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        decision = {
            "action": "propose_plan",
            "reasoning_summary": "only DEG has every required input",
            "feasibility": {
                "verdict": "answerable",
                "reasons": ["counts and metadata are present"],
                "missing_information": [],
            },
            "analysis_recommendations": ["differential"],
            "requires_approval": True,
            "requested_params": {},
        }
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(decision)}}],
        })

    case = next(case for case in load_golden_cases(DEFAULT_CASES_PATH) if case.case_id == "recommend_deg_001")
    assembly = EvalAssembly(
        name=EvalAssemblyName.OFFLINE,
        label="captured-live-model",
        available=True,
        skip_reason=None,
        model=VllmModelAdapter(
            base_url="http://model-host:8000",
            model="captured-live-model",
            client=httpx.Client(transport=httpx.MockTransport(handle)),
        ),
        fixture_tools=True,
        in_memory_state=True,
    )

    report = EvalRunner().run([case], assembly)

    sent_context = json.loads(captured["body"]["messages"][1]["content"])
    assert report.summary.passed == 1
    assert sent_context["available_input_roles"] == ["counts", "metadata"]
    assert sent_context["analysis_capabilities"] == [
        {"analysis_type": "differential", "display_label": "DEG", "required_inputs": ["counts", "metadata"]},
        {"analysis_type": "dem", "display_label": "DEM", "required_inputs": ["metabs", "metadata"]},
        {
            "analysis_type": "correlation",
            "display_label": "GMA",
            "required_inputs": ["transcriptome", "metabolome", "group"],
        },
    ]


def test_replay_diff_reports_new_failures_recoveries_and_metric_deltas() -> None:
    cases = load_golden_cases(DEFAULT_CASES_PATH)
    baseline = EvalRunner().run(cases, EvalAssemblyFactory.unit(label="baseline"))
    candidate = baseline.model_copy(deep=True)
    candidate.run_id = "candidate"
    candidate.model_label = "candidate-model"
    candidate.case_results[0].status = EvalCaseStatus.FAILED
    candidate.case_results[0].issues = ["route changed"]
    candidate.case_results[1].status = EvalCaseStatus.SKIPPED

    diff = compare_reports(baseline, candidate)

    assert diff.newly_failed == [candidate.case_results[0].case_id]
    assert diff.newly_skipped == [candidate.case_results[1].case_id]
    assert diff.newly_passed == []
    assert diff.pass_rate_delta < 0
    assert diff.baseline_run_id == baseline.run_id
    assert diff.candidate_run_id == "candidate"


def test_eval_cli_writes_machine_readable_report(tmp_path) -> None:
    from scripts.run_agent_eval import main

    output = tmp_path / "unit-report.json"
    assert main(["--assembly", "unit", "--output", str(output), "--label", "ci-stub"]) == 0

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["assembly"] == "unit"
    assert report["model_label"] == "ci-stub"
    assert report["summary"]["passed"] == 25


def test_eval_cli_writes_machine_readable_diff(tmp_path) -> None:
    from scripts.run_agent_eval import main

    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    diff_output = tmp_path / "candidate.diff.json"
    assert main(["--assembly", "unit", "--output", str(baseline), "--label", "baseline"]) == 0
    assert main([
        "--assembly", "unit",
        "--output", str(candidate),
        "--baseline", str(baseline),
        "--diff-output", str(diff_output),
        "--label", "candidate",
    ]) == 0

    diff = json.loads(diff_output.read_text(encoding="utf-8"))
    assert diff["newly_failed"] == []
    assert diff["newly_passed"] == []
    assert diff["newly_skipped"] == []
    assert diff["pass_rate_delta"] == 0
    assert diff["model_calls_delta"] == 0
