# Phase 7.5（S5）模型路由替换 RuleRouter

## 做了什么

- 新增 `ModelRouteDecision` 契约，限制意图、目标 profile、参数协商标记、置信度和原因长度。
- 新增 `ModelRouter`：模型分类失败、异常、非法 schema 或低置信度时回退 `RuleRouter`；模型结果经过 focus job、输入来源和待审批参数协商的服务端后置校验。
- `ProductionRunCoordinator` 保留可注入路由器，默认继续使用 `RuleRouter`，并将 `max_model_calls` 提升为 8，文档明确包含路由调用与 schema 修复预算。
- unit eval 的 5 个 router case 同时执行 RuleRouter 与 ModelRouter，结果写入 `router_accuracy_rule` / `router_accuracy_model`，并进入 diff 的 `metric_deltas`。
- 新增 S5 路由回归测试，覆盖非法 schema、低置信度、无 job 解读、无输入分析、待审批参数协商二次确认。

## DoD

- [x] 非法 intent/schema 回退 RuleRouter，不抛出异常
- [x] `confidence=low` 回退 RuleRouter
- [x] 无 focus job 的 `INTERPRET` 被改写为 `ASK_USER`
- [x] 无输入来源的 `ANALYZE` 被改写为 `DESCRIBE_ONLY/ANALYSIS`
- [x] 待审批状态的参数协商必须同时通过 RuleRouter 的服务端确认
- [x] 路由上下文（适配器注入路径）不包含工具清单，`available_tools=[]`
- [x] RuleRouter 保留为 fallback 与 eval 对照基线
- [x] unit eval 25/25；双路径指标均为 1.0
- [x] 后端 `216 passed, 6 skipped`
- [x] 前端测试 `10 passed`，生产构建成功

## 已知边界

- 当前生产默认路由仍为 `RuleRouter`；接入具备路由分类能力的模型适配器时通过 `ProductionRunCoordinator(router=...)` 注入 `ModelRouter`，原有模型适配器契约和 R6 降级路径不变。
- 前端构建仍报告既有 `InteractiveRouter` 大 chunk 警告，与本切片无关。
