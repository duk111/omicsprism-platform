from __future__ import annotations

from typing import Protocol, Sequence

from .schemas import GroundedAnswer, ToolResult, VerifierVerdict


class AnswerVerifier(Protocol):
    """无工具权限的回答核查接口。"""

    def verify(self, answer: GroundedAnswer, evidence: Sequence[ToolResult]) -> VerifierVerdict:
        ...
