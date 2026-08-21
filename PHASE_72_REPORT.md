# Phase 72 Report

## 目标与范围

S2 修复 correlation/GMA 计划在用户批准后提交必然返回 `preflight_blocked` 的缺陷。

- `submit_approved_plan` 仅对 differential/DEM 要求非空 `contrasts`。
- correlation 继续依赖 `run_preflight` 的 `can_submit` 和后续完整审批校验。
- 新增合法三输入 GMA 计划的审批提交与幂等重放测试。

本切片只改动 `backend/app/agent/tools.py` 的一处判断和 `backend/tests/test_phase3_tools.py` 的测试；S1 已有的工作区变更未回滚或覆盖。

## DoD 自查

- [x] 完整 GMA 计划（transcriptome + metabolome + group）经有效结构化审批后提交成功。
- [x] 相同 GMA `idempotency_key` 重放只创建一个 job。
- [x] differential 计划 contrasts 为空时仍返回 `preflight_blocked`（既有参数化回归）。
- [x] 未修改 `run_preflight` 的 can_submit 逻辑、plan hash、TTL、input source 或审批闸门。
- [x] 后端 pytest 无新增失败。
- [x] unit eval 保持 25/25。
- [x] 前端测试与构建通过。

## 红线自查

- R1：未修改模型上下文或模型输出边界。
- R2：未修改工具注册和 profile 白名单。
- R3：保留 source job 的 `get_for_user` 归属校验，未新增查询路径。
- R4：只收窄 correlation 的空 contrasts 特例；approval、plan_hash、TTL、input_source、fresh preflight、幂等 key 全部保留。
- R5：未修改 evidence、grounding 或 verifier。
- R6：未修改原有手工分析链路；本修复恢复 GMA 提交能力，不改变模型故障降级边界。

## 测试原始输出

修复前新增回归测试原始失败：

```text
FAILED test_approved_gma_plan_submits_once_and_replay_is_idempotent
AssertionError: first.ok is False; error_code='preflight_blocked'
```

修复后针对性测试：

```text
.venv/Scripts/python.exe -m pytest backend/tests/test_phase3_tools.py -q --basetemp .pytest-tmp/s2-pass-basetemp
.................                                                        [100%]
17 passed in 0.50s
```

后端全量回归：

```text
.venv/Scripts/python.exe -m pytest backend/tests -q --basetemp .pytest-tmp/s2-full-basetemp
..s..................................................................... [ 36%]
..................................................s...............ss.... [ 73%]
.........................................s..........s                    [100%]
191 passed, 6 skipped in 12.47s
```

Unit eval：

```text
.venv/Scripts/python.exe -m scripts.run_agent_eval --assembly unit --output .pytest-tmp/phase72-unit.json
"summary": {
  "total": 25,
  "passed": 25,
  "failed": 0,
  "skipped": 0,
  "pass_rate": 1.0,
  "metrics": {
    "schema_validity": 1.0,
    "route_accuracy": 1.0,
    "recommendation_accuracy": 1.0,
    "contrast_block_rate": 1.0,
    "unapproved_job_creations": 0.0,
    "cross_user_access_successes": 0.0,
    "numeric_accuracy": 1.0,
    "citation_coverage": 1.0
  }
}
```

前端回归：

```text
npm test -- --run
Test Files  2 passed (2)
Tests       10 passed (10)

npm run build
✓ built in 1m 4s
```

## 已知缺口与后续切片依赖

1. GMA 提交测试使用内存 fake stores；真实双服务器演示仍属于部署验收环境，不在本地 pytest 中执行。
2. S2 不改 correlation 的业务参数或 preflight 规则；后续切片可继续处理模型路由与控制流，但必须保持本修复的审批约束。
3. 按依赖顺序，下一步可进入 S3 system_facts 骨架。
