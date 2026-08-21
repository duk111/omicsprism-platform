# Phase 71 Report

## 目标与范围

S1 输入可见性已完成。模型上下文现在可以在严格边界内看到：

- 输入列总数与受限列名摘要（前 10 列 + 后 2 列）。
- counts、metabs、transcriptome、metabolome 的确定性 feature ID 抽样与非空 ID 总数。
- metadata/group 在不超过 60 行、10 列时的受限原始行摘要。

本切片只修改了 `backend/app/agent/schemas.py`、`backend/app/agent/tools.py`、`backend/app/agent/context.py` 和新增测试文件；没有修改 runtime、preflight、policy、router 或审批控制流。

## DoD 自查

- [x] 20,000 行 counts 产生最多 15 个 feature ID 样本，`feature_id_total=20000`。
- [x] 同一输入连续两次抽样结果完全一致。
- [x] 列摘要上限为 12，`column_count` 保留真实总数。
- [x] metadata raw rows 受 60 行 × 10 列 × 60 字符单元格限制；超限时为 `None`。
- [x] `InputInspectionSummary` 新字段有 Pydantic 长度/数值边界，raw row 内层最多 10 个单元格。
- [x] 输入摘要 JSON 大小测试通过，counts 与 metadata 均小于 4 KB。
- [x] 全量后端 pytest 无新增失败。
- [x] unit eval 保持 25/25。

## 红线自查

- R1：本切片只传有界摘要；feature ID 截断至 48 字符、最多 15 条，metadata raw rows 受多重上限约束；不传句柄、凭据、路径或完整 CSV。
- R2：未修改 profile 白名单或工具注册。
- R3：未修改资源查询与用户归属校验。
- R4：未修改审批、plan hash、job 创建入口或幂等逻辑。
- R5：未修改 evidence、grounding 或 verifier。
- R6：未修改原有业务链路、worker 或模型故障降级路径。

## 测试原始输出

命令：

```text
.venv/Scripts/python.exe -m pytest backend/tests/test_phase7_s1_input_visibility.py -q --basetemp .pytest-tmp/s1-basetemp2
....                                                                     [100%]
4 passed in 0.49s
```

```text
.venv/Scripts/python.exe -m pytest backend/tests -q --basetemp .pytest-tmp/s1-full-basetemp
..s..................................................................... [ 36%]
.................................................s...............ss..... [ 73%]
........................................s..........s                     [100%]
190 passed, 6 skipped in 11.61s
```

```text
.venv/Scripts/python.exe -m scripts.run_agent_eval --assembly unit --output .pytest-tmp/phase71-unit-final.json
"run_id": "4f32cf55-4de2-4231-9cf7-844fb3dad213",
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

前端回归（本切片未修改前端，但按项目常驻契约执行）：

```text
npm test -- --run
Test Files  2 passed (2)
Tests       10 passed (10)

npm run build
✓ built in 56.23s
```

## 已知缺口与后续依赖

1. S1 没有改 preflight 的样本对齐校验；该工作属于后续切片范围。
2. S1 没有改 runtime 控制流或模型路由。
3. 工作区中存在本切片之前的用户未提交变更，本切片未回滚或覆盖这些文件。
4. 下一切片按依赖顺序可进入 S2；S3 仍需在 S4 之前完成。
