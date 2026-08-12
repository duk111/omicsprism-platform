from __future__ import annotations

from typing import Protocol

from .schemas import ActiveProfile, RouteDecision, RouteIntent, RouteTargetProfile, RunState


class Router(Protocol):
    """Phase 2 才实现路由逻辑；Phase 0 只固定接口。"""

    def route(self, user_message: str, state: RunState) -> RouteDecision:
        ...


class RuleRouter:
    """基于意图和当前 focus 的确定性路由器，不访问业务工具。"""

    def route(self, user_message: str, state: RunState) -> RouteDecision:
        text = user_message.strip().lower()
        if not text:
            return RouteDecision(intent=RouteIntent.UNCLEAR, target_profile=RouteTargetProfile.ASK_USER, reason="empty message")

        rerun_terms = ("重跑", "重新运行", "rerun", "re-run", "重新分析")
        help_terms = ("你能做什么", "能做什么", "怎么使用", "使用帮助", "what can you do", "capabilities")
        analysis_terms = (
            "差异", "差异分析", "differential", "deg", "分析", "运行", "比较", "contrast",
            "analysis", "analyze", "analyse", "execute",
        )
        describe_terms = (
            "描述", "概览", "describe", "metadata", "元数据", "样本信息", "我有", "转录组", "代谢组",
            "上传", "传了", "文件已传", "uploaded", "attached",
        )
        interpret_terms = ("结果", "解释", "解读", "火山图", "富集", "evidence", "interpret")
        continuation_terms = ("继续", "下一步", "好的", "可以", "continue")
        attached_capability_terms = (
            "这个数据能做什么", "这些数据能做什么", "这两个数据能做什么",
            "这个文件能做什么", "这些文件能做什么", "这两个文件能做什么",
            "what can this data do", "what can these files do",
        )
        explain_plan_terms = (
            "plan是什么意思", "plan 是什么意思", "这个plan", "这个 plan",
            "计划是什么意思", "这个计划", "解释计划", "解释一下计划",
            "explain the plan", "what does this plan mean",
        )

        if any(term in text for term in explain_plan_terms):
            return RouteDecision(intent=RouteIntent.EXPLAIN_PLAN, target_profile=RouteTargetProfile.ANALYSIS, reason="explain current plan")
        if any(term in text for term in attached_capability_terms):
            return RouteDecision(intent=RouteIntent.DESCRIBE_ONLY, target_profile=RouteTargetProfile.ANALYSIS, reason="describe uploaded inputs")
        if any(term in text for term in help_terms):
            return RouteDecision(intent=RouteIntent.HELP, target_profile=RouteTargetProfile.ANALYSIS, reason="capability help")
        if any(term in text for term in rerun_terms):
            return RouteDecision(intent=RouteIntent.RERUN, target_profile=RouteTargetProfile.ANALYSIS, reason="rerun intent")
        if any(term in text for term in describe_terms) and not any(term in text for term in analysis_terms):
            return RouteDecision(intent=RouteIntent.DESCRIBE_ONLY, target_profile=RouteTargetProfile.ANALYSIS, reason="analysis consultation")
        if any(term in text for term in interpret_terms) and state.focus.in_scope_job_ids:
            return RouteDecision(intent=RouteIntent.INTERPRET, target_profile=RouteTargetProfile.INTERPRETATION, reason="focused result intent")
        if any(term in text for term in analysis_terms):
            return RouteDecision(intent=RouteIntent.ANALYZE, target_profile=RouteTargetProfile.ANALYSIS, reason="analysis intent")
        if any(term in text for term in interpret_terms):
            return RouteDecision(intent=RouteIntent.INTERPRET, target_profile=RouteTargetProfile.ASK_USER, reason="no result in focus")
        if any(term in text for term in continuation_terms):
            target = RouteTargetProfile(state.active_profile.value)
            intent = RouteIntent.ANALYZE if state.active_profile is ActiveProfile.ANALYSIS else RouteIntent.INTERPRET
            return RouteDecision(intent=intent, target_profile=target, reason="continue current profile")
        return RouteDecision(intent=RouteIntent.UNCLEAR, target_profile=RouteTargetProfile.ANALYSIS, reason="bounded biology consultation")
