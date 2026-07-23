from __future__ import annotations

from .schemas import AgentAction, AgentDecision, AgentState, FeasibilityVerdict, RunState


class InvalidDecision(ValueError):
    pass


class DecisionValidator:
    def validate(self, state: RunState, decision: AgentDecision) -> None:
        if state.state is AgentState.CHECK_INPUTS:
            if decision.action is not AgentAction.PROPOSE_PLAN:
                raise InvalidDecision("CHECK_INPUTS requires PROPOSE_PLAN")
            if decision.feasibility is None:
                raise InvalidDecision("PROPOSE_PLAN requires feasibility")
            if decision.feasibility.verdict is FeasibilityVerdict.NOT_ANSWERABLE:
                raise InvalidDecision("not answerable cannot proceed to proposal")
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
