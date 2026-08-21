# Phase 7.7（S7）参数推断交给模型，代码只做存在性校验

## 做了什么

- `AgentAnalysisPlanDecision` / `AgentDecision` 增加有界 `inference_note`，`AgentPlanBlock` 将其展示在审批计划中。
- `_complete_contrast_params` 对模型或用户提供的完整 contrast 参数执行服务端确定性校验：metadata 列存在、tested/reference 水平存在、每组样本数满足 `min_replicates`。
- 保留 `_REFERENCE_LEVEL_MARKERS`，仅在字段缺失且模型推断不可用时作为 fallback；不再用关键词跳过完整参数校验。
- 校验失败回到 `NEED_USER_INPUT` 追问路径，不生成计划、审批或 job；preflight 和提交链保持不变。
- 新增 S7 测试覆盖 `NaCl/Normal`、非法列/水平和 replicate 不足。

## DoD

- [x] `Normal` 等非 marker reference level，只要真实存在且满足 replicate 即可接受
- [x] 模型推断不存在的 metadata 列被拦截
- [x] 模型推断不存在的 group level 被拦截
- [x] replicate 数不足被拦截
- [x] fallback marker 逻辑保留
- [x] inference note 出现在 plan block，长度最多 200 字符
- [x] 后端 `225 passed, 6 skipped`
- [x] unit eval `25/25`

## 已知边界

- 推断说明是基于当前 bounded metadata summary 的审阅提示，不是未经验证的结果结论；最终可提交性仍由 preflight 和审批闸门决定。
