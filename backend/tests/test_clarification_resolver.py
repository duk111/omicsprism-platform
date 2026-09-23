from __future__ import annotations

import pytest

from backend.app.agent.clarification_resolver import (
    ClarificationOption,
    ClarificationResolver,
    ClarificationResolverInput,
    param_specs_for_analysis,
    stable_option_id,
)
from backend.app.agent.graph import (
    AgentDecision,
    GraphState,
    PendingAnalysisClarification,
)
from backend.app.agent.nodes.analysis_agent import analysis_agent_node
from backend.app.agent.param_resolver import AnalysisProposal, ScopeSpec


def _pending(
    *,
    analysis_type: str = "DEG",
    proposal: AnalysisProposal | None = None,
    options: list[ClarificationOption] | None = None,
) -> PendingAnalysisClarification:
    return PendingAnalysisClarification(
        analysis_type=analysis_type,
        question="Please choose the analysis parameters.",
        missing=["contrast"],
        options=options or [],
        source_message="run an analysis",
        source_action="run_analysis",
        proposal=proposal,
    )


def _state(message: str, pending: PendingAnalysisClarification) -> GraphState:
    return GraphState(
        thread_id="clarification-thread",
        user_id="user-1",
        user_message=message,
        pending_analysis=pending,
    )


def test_param_specs_are_introspected_and_scoped_by_analysis_type() -> None:
    deg = param_specs_for_analysis("DEG")
    dem = param_specs_for_analysis("DEM")
    gma = param_specs_for_analysis("GMA")

    assert set(deg) == {"padj_cutoff", "log2fc_cutoff", "min_total_count", "min_replicates"}
    assert set(dem) == {
        "padj_cutoff",
        "log2fc_cutoff",
        "vip_cutoff",
        "min_replicates",
        "max_missing_fraction",
        "impute_method",
    }
    assert set(gma) == {"fdr_cutoff", "max_missing_fraction"}
    assert deg["padj_cutoff"].default == 0.05
    assert deg["padj_cutoff"].ge == 0
    assert deg["padj_cutoff"].le == 1
    assert gma["fdr_cutoff"].default == 0.05


def test_deterministic_resolver_maps_multiple_threshold_edits() -> None:
    result = ClarificationResolver()(ClarificationResolverInput(
        user_reply="set min replicates to 1 and widen log2FC to 0.5",
        param_spec=param_specs_for_analysis("DEG"),
    ))

    assert result.intent == "edit_params"
    assert result.proposal_patch == {"min_replicates": 1, "log2fc_cutoff": 0.5}


def test_option_ids_are_stable_and_ordinal_selection_returns_patch() -> None:
    option = ClarificationOption(
        option_id=stable_option_id("salt vs control", {"tested_level": "salt"}),
        label="salt vs control",
        proposal_patch={"tested_level": "salt", "reference_level": "control"},
    )
    request = ClarificationResolverInput(
        user_reply="the second one",
        options=[
            ClarificationOption(option_id="first", label="control vs salt"),
            option,
        ],
    )

    result = ClarificationResolver()(request)

    assert result.intent == "edit_params"
    assert result.matched_option_id == option.option_id
    assert result.proposal_patch == {"tested_level": "salt", "reference_level": "control"}
    assert option.option_id == stable_option_id("salt vs control", {"tested_level": "salt"})


def test_param_question_preserves_active_pending_state() -> None:
    pending = _pending(
        proposal=AnalysisProposal(analysis_type="DEG", scope=ScopeSpec(mode="all")),
    )
    result = analysis_agent_node(lambda *_args, **_kwargs: None)(_state(
        "what does padj_cutoff mean?", pending,
    ))

    assert result["decision"].action == "ask_user"
    assert result["pending_analysis"].status == "active"
    assert "padj_cutoff" in result["response_text"]


def test_unrelated_pending_reply_explicitly_reroutes_to_qa() -> None:
    pending = _pending(
        proposal=AnalysisProposal(analysis_type="DEG", scope=ScopeSpec(mode="all")),
    )
    result = analysis_agent_node(lambda *_args, **_kwargs: None)(_state(
        "tell me a biology fact", pending,
    ))

    assert result["decision"] == AgentDecision(action="reroute", reroute_to="qa")
    assert result["pending_analysis"].status == "active"


def test_role_reroute_contract_accepts_an_explicit_target() -> None:
    from backend.app.agent.graph import AnalysisDecision

    decision = AnalysisDecision(action="reroute", reroute_to="qa")

    assert decision.reroute_to == "qa"


def test_role_reroute_contract_rejects_same_role_or_missing_target() -> None:
    from pydantic import ValidationError
    from backend.app.agent.graph import QaDecision

    with pytest.raises(ValidationError):
        QaDecision(action="reroute", reroute_to="qa")
    with pytest.raises(ValidationError):
        QaDecision(action="reroute")


def test_cancel_marks_pending_terminal_without_submission_action() -> None:
    result = analysis_agent_node(lambda *_args, **_kwargs: None)(_state(
        "cancel this analysis", _pending(),
    ))

    assert result["decision"].action == "ask_user"
    assert result["pending_analysis"].status == "cancelled"


def test_typed_confirmation_consumes_pending_before_analysis_node() -> None:
    result = analysis_agent_node(lambda *_args, **_kwargs: None)(_state(
        "confirm and continue",
        _pending(proposal=AnalysisProposal(analysis_type="DEG", scope=ScopeSpec(mode="all"))),
    ))

    assert result["decision"].action == "run_analysis"
    assert result["pending_analysis"].status == "consumed"


def test_gma_threshold_edit_does_not_require_contrast() -> None:
    result = analysis_agent_node(lambda *_args, **_kwargs: None)(_state(
        "change fdr to 0.1", _pending(
            analysis_type="GMA",
            proposal=AnalysisProposal(analysis_type="GMA"),
        ),
    ))

    assert result["decision"].action == "run_analysis"
    assert result["decision"].proposal.requested_params["fdr_cutoff"] == 0.1


def test_invalid_threshold_patch_is_rejected_before_analysis_resolution() -> None:
    result = analysis_agent_node(lambda *_args, **_kwargs: None)(_state(
        "set padj_cutoff to 2",
        _pending(proposal=AnalysisProposal(analysis_type="DEG", scope=ScopeSpec(mode="all"))),
    ))

    assert result["decision"].action == "ask_user"
    assert result["pending_analysis"].status == "active"
    assert "校验" in result["response_text"]
