from __future__ import annotations

import re
from typing import Any, Callable, Protocol

from .schemas import (
    ActiveProfile,
    AgentState,
    ModelContext,
    ModelRouteDecision,
    RouteDecision,
    RouteIntent,
    RouteTargetProfile,
    RunState,
)


class Router(Protocol):
    """Phase 2 才实现路由逻辑；Phase 0 只固定接口。"""

    def route(self, user_message: str, state: RunState) -> RouteDecision:
        ...


class RuleRouter:
    """基于意图和当前 focus 的确定性路由器，不访问业务工具。

    检查顺序即优先级：更具体的意图（解释计划、能力、帮助、重跑、查状态）
    必须先于宽泛的分析/解读意图，避免「分析」等词吞掉其它意图。

    新增关键词规则时必须说明它在什么状态下生效：宽词（如「状态」「进度」）一律
    要有状态门（state.focus / state.state），避免把纯咨询意图吞进分析/状态流程。

    表达「补充/修改分析参数」的规则必须置 is_param_negotiation=True：下游用它
    （而不是 reason 文案）决定是否作废待批计划、是否保留 draft_params。
    """

    def route(self, user_message: str, state: RunState) -> RouteDecision:
        text = user_message.strip().lower()
        if not text:
            return RouteDecision(intent=RouteIntent.UNCLEAR, target_profile=RouteTargetProfile.ASK_USER, reason="empty message")

        rerun_terms = ("重跑", "重新运行", "rerun", "re-run", "重新分析")
        help_terms = ("你能做什么", "能做什么", "怎么使用", "使用帮助", "what can you do", "capabilities")
        describe_terms = (
            "描述", "概览", "describe", "metadata", "元数据", "样本信息", "我有", "转录组", "代谢组",
            "上传", "传了", "文件已传", "uploaded", "attached",
        )
        interpret_terms = ("结果", "解释", "解读", "火山图", "富集", "evidence", "interpret")
        # 状态词分强弱：强词本身只有查状态一个意思，无条件命中；
        # 弱词（状态/进度/done 等）只在 focus 有任务时才算状态查询，否则是咨询。
        strong_status_terms = (
            "跑完了", "完成了吗", "出结果了吗", "结果出来了吗", "结果好了吗",
            "结束了吗", "跑完了吗",
        )
        weak_status_terms = ("状态", "进度", "progress", "status", "done")
        # 解读动词优先于状态词：例如「结果好了吗，顺便解读一下 top 基因」是解读诉求。
        interpret_priority_terms = ("解读", "解释一下结果", "分析结果", "interpret")
        # 参数调整必须有明确的修改动作 + 分析参数语境，避免把「描述对照组」等吞掉。
        adjust_terms = ("改成", "改为", "换成", "换一下", "换一个", "调整为", "设为", "改一下", "重设")
        param_context_terms = ("比较", "对照", "实验组", "对照组", "阈值", "padj", "log2fc", "min_replicates", "min_total_count")
        # 参数答案只在本轮协商中生效（待补参数或已有 draft），否则是实验设计咨询。
        param_answer_terms = ("比较列", "实验组", "对照组", "分组列", "组别", "阈值", "padj", "log2fc")
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
        # 解读诉求优先：状态词不应抢走「顺便解读一下」的解读意图。
        if any(term in text for term in interpret_priority_terms) and state.focus.in_scope_job_ids:
            return RouteDecision(intent=RouteIntent.INTERPRET, target_profile=RouteTargetProfile.INTERPRETATION, reason="interpretation intent over status")
        if any(term in text for term in strong_status_terms):
            return RouteDecision(intent=RouteIntent.CHECK_STATUS, target_profile=RouteTargetProfile.ANALYSIS, reason="job status intent")
        if any(term in text for term in weak_status_terms) and state.focus.in_scope_job_ids:
            return RouteDecision(intent=RouteIntent.CHECK_STATUS, target_profile=RouteTargetProfile.ANALYSIS, reason="job status intent")
        if any(term in text for term in interpret_terms) and state.focus.in_scope_job_ids:
            return RouteDecision(intent=RouteIntent.INTERPRET, target_profile=RouteTargetProfile.INTERPRETATION, reason="focused result intent")
        if any(term in text for term in describe_terms) and not _has_analysis_terms(text):
            return RouteDecision(intent=RouteIntent.DESCRIBE_ONLY, target_profile=RouteTargetProfile.ANALYSIS, reason="analysis consultation")
        if any(term in text for term in adjust_terms) and any(term in text for term in param_context_terms):
            return RouteDecision(
                intent=RouteIntent.ANALYZE,
                target_profile=RouteTargetProfile.ANALYSIS,
                reason="parameter adjustment intent",
                is_param_negotiation=True,
            )
        # 参数答案（多轮协商里用户补充比较列/实验组/对照组/阈值）应回到计划生成流程，
        # 但只在协商语境（待补参数或已有 draft）里生效，避免吞掉实验设计咨询。
        if any(term in text for term in param_answer_terms) and (
            state.state is AgentState.NEED_USER_INPUT or bool(state.focus.draft_params)
        ):
            return RouteDecision(
                intent=RouteIntent.ANALYZE,
                target_profile=RouteTargetProfile.ANALYSIS,
                reason="parameter answer intent",
                is_param_negotiation=True,
            )
        if _has_analysis_terms(text):
            return RouteDecision(intent=RouteIntent.ANALYZE, target_profile=RouteTargetProfile.ANALYSIS, reason="analysis intent")
        if any(term in text for term in interpret_terms):
            return RouteDecision(intent=RouteIntent.INTERPRET, target_profile=RouteTargetProfile.ASK_USER, reason="no result in focus")
        if _is_continuation(text):
            target = RouteTargetProfile(state.active_profile.value)
            intent = RouteIntent.ANALYZE if state.active_profile is ActiveProfile.ANALYSIS else RouteIntent.INTERPRET
            return RouteDecision(intent=intent, target_profile=target, reason="continue current profile")
        return RouteDecision(intent=RouteIntent.UNCLEAR, target_profile=RouteTargetProfile.ANALYSIS, reason="bounded biology consultation")


class ModelRouter:
    """模型主路由；失败、低置信度和破坏性信号不确定时回退 RuleRouter。"""

    def __init__(self, model: Any = None, fallback: RuleRouter | None = None,
                 has_inputs: Callable[[RunState], bool] | None = None,
                 *, classify: Callable[[str, RunState], Any] | None = None) -> None:
        # `classify` 保留给 unit fixture；生产注入使用 model adapter。两者都只能返回
        # ModelRouteDecision，不携带工具、参数或资源句柄。
        self.model = model if model is not None else classify
        if self.model is None:
            raise TypeError("ModelRouter requires a model or classify callable")
        self.fallback = fallback or RuleRouter()
        self.has_inputs = has_inputs or _available_input_roles

    def route(self, user_message: str, state: RunState) -> RouteDecision:
        fallback = self.fallback.route(user_message, state)
        try:
            model_route = ModelRouteDecision.model_validate(self._classify(user_message, state))
        except Exception:
            return fallback
        if model_route.confidence == "low":
            return fallback
        target = model_route.target_profile
        intent = model_route.intent
        if intent is RouteIntent.INTERPRET and not state.focus.in_scope_job_ids:
            target = RouteTargetProfile.ASK_USER
        if intent is RouteIntent.ANALYZE and not self.has_inputs(state):
            intent = RouteIntent.DESCRIBE_ONLY
            target = RouteTargetProfile.ANALYSIS
        negotiation = model_route.is_param_negotiation
        if state.state is AgentState.WAIT_EXECUTION_CONFIRMATION:
            negotiation = negotiation and fallback.is_param_negotiation
        return RouteDecision(
            intent=intent,
            target_profile=target,
            reason=model_route.reason,
            is_param_negotiation=negotiation,
        )

    def _classify(self, user_message: str, state: RunState) -> Any:
        if callable(self.model):
            return self.model(user_message, state)
        classify = getattr(self.model, "classify", None)
        if callable(classify):
            return classify(user_message, state)
        # Adapter implementations may expose a dedicated route method. A normal
        # AgentDecision from `decide` is deliberately rejected by schema validation.
        route = getattr(self.model, "route", None)
        if callable(route):
            return route(user_message, state)
        decide = getattr(self.model, "decide", None)
        if callable(decide):
            context = ModelContext(
                user_message=user_message,
                active_profile=state.active_profile,
                state=state.state,
                in_scope_job_ids=list(state.focus.in_scope_job_ids),
                conversation_summary=None,
                available_input_roles=[],
                available_tools=[],
            )
            return decide(context)
        raise TypeError("model adapter does not expose route classification")


def _available_input_roles(state: RunState) -> bool:
    """路由态只持有来源指纹/草稿；有输入来源指纹即可认为输入已绑定。"""
    return bool(
        state.focus.params_source_ref
        or state.focus.draft_params
        or state.plan_id
        or state.pending_approval_id
    )


_CONTINUATION_MATCHES = {"继续", "继续吧", "下一步", "好的", "好", "可以", "ok", "continue", "go on"}


def _is_continuation(text: str) -> bool:
    """完全匹配的延续词；去掉首尾标点/空白，内部标点不算（避免吞掉长句）。"""
    stripped = text.strip(" \t\r\n，。！？、；：,.!?;:'\"()（）[]【】")
    return stripped in _CONTINUATION_MATCHES


# 「比较」单独做边界匹配：避免「比较好/比较合适」等比较级误命中分析意图。
# 这是启发式补语集，覆盖常见比较级搭配；不在集内则仍按分析意图处理。
_COMPARE_PATTERN = re.compile(r"比较(?!好|合适|常见|常用|方便|容易|简单|稳定|高效|可靠|快|慢|多|少|大|小|高|低)")

_ANALYSIS_TERMS = (
    "差异", "差异分析", "differential", "deg", "分析", "运行", "contrast",
    "analysis", "analyze", "analyse", "execute",
)


def _has_analysis_terms(text: str) -> bool:
    if any(term in text for term in _ANALYSIS_TERMS):
        return True
    return bool(_COMPARE_PATTERN.search(text))
