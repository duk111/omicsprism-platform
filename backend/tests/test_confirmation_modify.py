from __future__ import annotations

from backend.app.agent.clarification_resolver import ClarificationResolver, ClarificationResolverInput, param_specs_for_analysis


def test_confirmation_modify_is_english_and_deterministic_for_gma() -> None:
    result = ClarificationResolver()(ClarificationResolverInput(
        user_reply="set fdr cutoff to 0.2",
        param_spec=param_specs_for_analysis("GMA"),
    ))

    assert result.intent == "edit_params"
    assert result.proposal_patch == {"fdr_cutoff": 0.2}


def test_confirmation_text_is_deterministic_for_english_confirm() -> None:
    result = ClarificationResolver()(ClarificationResolverInput(
        user_reply="confirm",
        param_spec=param_specs_for_analysis("GMA"),
    ))

    assert result.intent == "confirm"
