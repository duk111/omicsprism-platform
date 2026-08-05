from __future__ import annotations

from .schemas import AgentAction, AgentDecision, AgentState, FeasibilityVerdict, RunState


class InvalidDecision(ValueError):
    pass


class DecisionValidator:
    def validate(self, state: RunState, decision: AgentDecision) -> None:
        if decision.grounded_answer is not None and decision.action is not AgentAction.ANSWER:
            raise InvalidDecision("grounded answer is only valid for ANSWER")
        if decision.advisory_answer is not None and state.state is not AgentState.ADVISE:
            raise InvalidDecision("advisory answer is only valid in ADVISE")
        if state.state is AgentState.ADVISE:
            if decision.action is not AgentAction.ANSWER or not decision.advisory_answer:
                raise InvalidDecision("ADVISE requires an advisory ANSWER")
            if (
                decision.grounded_answer is not None
                or decision.feasibility is not None
                or decision.analysis_recommendations
                or decision.requested_params
                or decision.requires_approval
            ):
                raise InvalidDecision("ADVISE cannot produce evidence, plans, parameters, or approval")
        elif state.state is AgentState.CHECK_INPUTS:
            if decision.action is AgentAction.REQUEST_MORE_DATA:
                if decision.feasibility is None or decision.feasibility.verdict is not FeasibilityVerdict.NOT_ANSWERABLE:
                    raise InvalidDecision("REQUEST_MORE_DATA requires a not-answerable feasibility result")
                if decision.analysis_recommendations or decision.requested_params or decision.requires_approval:
                    raise InvalidDecision("REQUEST_MORE_DATA cannot recommend, parameterize, or request approval")
            elif decision.action is AgentAction.PROPOSE_PLAN:
                if decision.feasibility is None:
                    raise InvalidDecision("PROPOSE_PLAN requires feasibility")
                if decision.feasibility.verdict is FeasibilityVerdict.NOT_ANSWERABLE:
                    raise InvalidDecision("not answerable cannot proceed to proposal")
            else:
                raise InvalidDecision("CHECK_INPUTS requires PROPOSE_PLAN or REQUEST_MORE_DATA")
        elif state.state is AgentState.WAIT_PLAN_CONFIRMATION:
            if decision.action is not AgentAction.REQUEST_APPROVAL:
                raise InvalidDecision("WAIT_PLAN_CONFIRMATION requires REQUEST_APPROVAL")
        elif state.state is AgentState.ANSWER_WITH_EVIDENCE:
            if decision.action is not AgentAction.ANSWER:
                raise InvalidDecision("ANSWER_WITH_EVIDENCE requires ANSWER")
        elif decision.action is AgentAction.PROPOSE_PLAN and decision.feasibility is None:
            raise InvalidDecision("PROPOSE_PLAN requires feasibility")
        if decision.feasibility and decision.feasibility.verdict is FeasibilityVerdict.NOT_ANSWERABLE:
            if decision.action is not AgentAction.REQUEST_MORE_DATA:
                raise InvalidDecision("not answerable may only request more data")
