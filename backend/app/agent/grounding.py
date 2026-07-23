from __future__ import annotations

from typing import Protocol

from .schemas import GroundedAnswer, ToolResult


class EvidenceGrounder(Protocol):
    """证据约束接口；本阶段不生成回答。"""

    def ground(self, evidence: ToolResult) -> GroundedAnswer:
        ...
