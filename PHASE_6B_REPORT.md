# Phase 6 Gate B 报告：生产协调器与 agent worker

## 当前状态

Gate B 代码、本地测试、Phase 5 回归、服务器真实 PostgreSQL 定向测试和修复后的完整回归均已完成。Gate B 只等待人工红线审阅，尚未切换 Gate C。

## 已完成工作

- 实现 `ProductionRunCoordinator`，有界执行 router、model、profile policy、tool、approval、grounding 和 verifier；预算为最多 8 次状态转换、3 次模型调用、6 次工具调用和 90 秒，且调用预算在真实调用前检查。
- 分析闭环固定执行 inspect → model recommendation → preflight → plan/approval；只有结构化审批有效、未过期且 plan hash 匹配时才能提交真实 job。
- 解读闭环先生成受控 evidence query，再调用 `query_result_evidence`，最后用 citation row ids 反查本轮返回行并复核数字；空证据使用固定模板且不发起第二次模型调用。
- 实现 ownership-bound `ExistingJobInputSource` 与 `StagedBundleInputSource`；staged bundle 同时绑定 user 与 thread，并在读取时复核 checksum，审批通过前不复制输入、不创建 job。
- 实现串行 `backend.agent_worker`：PostgreSQL claim、lease/attempt recovery、`FOR UPDATE SKIP LOCKED` 和全局 advisory lock；模型只在 worker 进程装配。
- 实现 PostgreSQL 原子 checkpoint：同一事务内乐观锁更新 `agent_runs`、追加 assistant message/event 并完成 turn；冲突时整个事务回滚，等待 lease recovery。
- 将模型连接失败、超时、HTTP 拒绝、非法 schema、状态冲突和预算超限映射为稳定错误码；不把模型异常传播到 API 或原分析 worker。

## 测试先行证据

Gate B 红线测试首次在实现前按预期于收集阶段失败：

```text
ImportError: cannot import name 'ProductionRunCoordinator'
ModuleNotFoundError: No module named 'backend.agent_worker'
```

错误分类补测在实现调整前按预期暴露了两个通用错误码：

```text
ConnectError: expected model_unavailable, got agent_turn_failed
HTTPStatusError(400): expected model_request_rejected, got agent_turn_failed
2 failed, 8 passed
```

修复后的 Gate B 定向测试：

```text
16 passed, 2 skipped in 2.66s
```

两个 skip 都来自 `test_phase6b_db_worker.py`，需要专用 PostgreSQL 测试库。

## 本地验证

```text
python -m pytest backend/tests -q -rs --basetemp .pytest-phase6b-final
119 passed, 5 skipped in 3.58s

python -m compileall -q backend/app backend/agent_worker.py scripts
compileall exit: 0

python -m scripts.run_agent_eval --assembly unit
25 passed, 0 failed, 0 skipped
pass_rate 1.0
model_calls 4
route_accuracy 1.0
recommendation_accuracy 1.0
contrast_block_rate 1.0
unapproved_job_creations 0.0
cross_user_access_successes 0.0
numeric_accuracy 1.0
citation_coverage 1.0

git diff --check
passed
```

五个 skip 均为真实 PostgreSQL 权限或 repository 测试；本报告不声称这些测试已在本地通过。

## Gate B DoD

- [x] `ProductionRunCoordinator` 只通过注入的 store/model/tool 接口工作；模型上下文测试证明不含 DB、shell、凭据、路径、storage key 或原始 CSV。
- [x] 分析/解读 profile 继续由 `ProfilePolicyGuard` 结构化白名单约束；解读上下文和 executor 不包含 `submit_approved_plan`。
- [x] grounded answer 按 citation row ids 反查本轮 evidence rows 并复核数字；空证据固定回答“没有满足阈值的证据”且只调用模型一次。
- [x] 内存 production repositories 已跑通 analyze → approve → submit → job card 与 interpret → citation；staged bundle 测试证明审批前 0 job、审批后恰好 1 job、重放不重复。
- [x] worker 单元测试覆盖全局串行、过期 lease recovery、稳定模型错误和原子 checkpoint 调用；乐观锁冲突重放不重复创建或 enqueue job。
- [x] Phase 5 的 25-case unit replay 为 25/25，全部安全指标零回归。
- [x] 服务器真实 PostgreSQL claim/lease/advisory lock/atomic checkpoint 与完整审批提交测试为 5/5 通过。
- [x] 修复后的服务器完整 backend tests 为 134/134 通过，compileall 通过。
- [ ] Gate B R1/R2/R4/R5/worker trace 人工审阅待完成。

## 服务器验证命令

在仓库根目录沿用 Gate A 的专用测试库凭据：

```bash
export OMICS_PRISM_MIGRATION_DATABASE_URL='postgresql://migration_admin:<admin-password>@<host>:<port>/<test-db>'
export OMICS_PRISM_TEST_DATABASE_URL="$OMICS_PRISM_MIGRATION_DATABASE_URL"
export OMICS_PRISM_TEST_APP_DATABASE_URL='postgresql://omics_app:<runtime-password>@<host>:<port>/<test-db>'
export OMICS_PRISM_APP_DB_PASSWORD='<runtime-password>'

python3 scripts/migrate.py
python3 -m pytest \
  backend/tests/test_agent_db_permissions.py \
  backend/tests/test_runtime_database_permissions.py \
  backend/tests/test_phase6a_db_permissions.py \
  backend/tests/test_phase6b_db_worker.py \
  -q -rs
```

预期 `test_phase6b_db_worker.py` 执行两个真实数据库用例：

1. claim、未过期 lease 不可抢占、过期 lease recovery、全局 advisory lock 和单事务 checkpoint。
2. 真实 state/plan/approval/event/product repositories 配合 stub model 完成分析 plan；审批前 0 job，结构化批准后返回 job block 并恰好 save/enqueue 一次。

不要把 DSN 或密码贴进报告；只补 pytest 汇总和人工审阅结论。

### 服务器输出

2026-07-30 在 worker 所在服务器执行真实 PostgreSQL 定向测试：

```text
python3 scripts/migrate.py

python3 -m pytest \
  backend/tests/test_agent_db_permissions.py \
  backend/tests/test_runtime_database_permissions.py \
  backend/tests/test_phase6a_db_permissions.py \
  backend/tests/test_phase6b_db_worker.py \
  -q -rs
..... [100%]
5 passed in 27.25s
```

首次完整回归为 `131 passed, 3 failed`。其中：

- `test_phase6b_worker.py` 暴露 Python 3.10 不接受 ISO `Z` 后缀，已改为先经 Pydantic 解析 lease 时间再比较。
- `test_phase2_control_plane.py` 仍断言 Phase 2 旧上下文字段集合，已同步 Gate A/B 新增的 `available_input_roles`、`analysis_capabilities` 和 `evidence`。
- `test_agent_db_permissions.py` 的 focused 验证已通过，但完整套件中重复建立 app-role 连接时发生一次 `ConnectionTimeout`；已在保持全部权限断言的前提下合并为单一 app-role 连接。

上述首次失败不记作通过。拉取修复提交 `2ddfb5c` 后重新执行：

```text
python3 -m pytest backend/tests -q -rs
134 passed, 1 warning in 23.53s

python3 -m compileall -q backend/app backend/agent_worker.py scripts
无输出，exit code 0
```

唯一 warning 是既有 FastAPI `TestClient` 对 Starlette/httpx 兼容层的弃用提示，不影响 Gate B 行为或安全结论。

### 人工审阅

待人工确认：

- R1：worker 发往模型的上下文无句柄、凭据、路径/storage key 或原始 CSV。
- R2：interpretation profile 结构上无写工具。
- R4：自然语言不能恢复审批；staged/existing input 均为批准前 0 job、批准后恰好 1 job。
- R5：grounded 数字与 citation row ids 能追到本轮 evidence rows，空结果不回退模型文本。
- worker trace：一个 turn 的 run checkpoint、assistant message、events 和 completed 状态同事务提交；冲突时全部回滚并由 lease 恢复。

## 已知缺口

- 本地没有专用 PostgreSQL；Gate B 的两个真实数据库用例和完整测试集已在服务器通过。
- 业务 job 的持久化与 Redis publish 沿用现有 OmicsPrism 提交流程；跨 PostgreSQL/Redis 的事务 outbox 不属于 Gate B，本 Gate 依靠确定性 job id、plan submitted ids 与现有 job 状态幂等收敛。
- HTTP/SSE、`/copilot` 前端和部署闭环分别属于 Gate C、D、E，当前按契约未实现。
