# Phase 6 Gate E 报告：双服务器端到端闭环

> 状态：已完成并关闭。2026-08-13，四项双服务器生产演示均已通过人工验收；Phase 6 的汇总结论见 `PHASE_6_REPORT.md`。

## 1. 本 Gate 范围

- 固化当前双服务器部署：云服务器运行 nginx/API/PostgreSQL/Redis/MinIO，算力服务器运行 analysis worker、agent worker 与 vLLM。
- 将算力服务器 agent worker compose 纳入版本控制，不再依赖服务器上的未跟踪文件。
- 在真实 PostgreSQL、Redis、MinIO、vLLM 和两个 worker 上完成四项演示，并保留可复现命令与人工结论。
- 不新增业务工具、模型能力、管理员 UI、通用 RAG/SQL/shell 或 token 级流式输出。

## 2. 已完成的真实生产验收

### E1：新上传 DEG → plan → approve → job

- 用户从公网 `/omicsprism/copilot` 上传 counts 与 metadata。
- Copilot 使用真实 metadata 列和值生成 DEG plan；审批前未创建 job。
- 用户点击结构化 `Approve plan` 后创建并入队一个 differential job。
- analysis worker 从 Redis 取走任务，PostgreSQL 最终状态为 `succeeded`，Copilot job 卡显示 100% 并可打开结果页。

人工结论：通过。

### E2：已有结果 → evidence → citation

- 用户在同一 thread 询问“结果是什么意思”。
- interpretation profile 先查询白名单结果表，再基于当前 evidence 生成回答。
- vLLM 两阶段请求均返回 200；回答经过数字/row id/citation 校验后展示。
- 生产中发现的第二阶段 8192-token 上下文 400 已通过 12 行/12 KB 有界证据修复，复验通过。

人工结论：通过。

## 3. 已完成：E3 跨用户全资源 404

先从生产 PostgreSQL 选择同一用户 A 的一组现有资源，只输出 ID，不输出 payload、storage key 或凭据：

```bash
sudo docker exec omicsprism-postgres-1 \
  psql -U postgres -d omicsprism -At -F '|' -c "
    SELECT t.user_id,
           t.thread_id,
           b.bundle_id,
           a.approval_id,
           j.id AS job_id
    FROM agent_threads t
    LEFT JOIN LATERAL (
      SELECT bundle_id FROM agent_input_bundles
      WHERE user_id = t.user_id AND thread_id = t.thread_id
      ORDER BY created_at DESC LIMIT 1
    ) b ON true
    LEFT JOIN LATERAL (
      SELECT approval_id FROM agent_approvals
      WHERE user_id = t.user_id AND thread_id = t.thread_id
      ORDER BY created_at DESC LIMIT 1
    ) a ON true
    LEFT JOIN LATERAL (
      SELECT id FROM jobs
      WHERE owner_id = t.user_id
      ORDER BY created_at DESC LIMIT 1
    ) j ON true
    WHERE b.bundle_id IS NOT NULL
      AND a.approval_id IS NOT NULL
      AND j.id IS NOT NULL
    ORDER BY t.updated_at DESC
    LIMIT 1;"
```

在云服务器建立全新的用户 B Cookie，并创建 B 自己的 thread：

```bash
PUBLIC_API='http://127.0.0.1:18086/api'
COOKIE_B='/tmp/omicsprism-gate-e-user-b.cookie'

curl -fsS -c "$COOKIE_B" "$PUBLIC_API/agent/threads"

B_THREAD_ID="$(curl -fsS -b "$COOKIE_B" -c "$COOKIE_B" \
  -H 'Content-Type: application/json' \
  -d '{"focus_job_ids":[]}' \
  "$PUBLIC_API/agent/threads" |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["thread_id"])')"
```

将上一步查询到的 ID 填入下列变量。四个请求必须全部返回普通 `404`，响应不得区分“不存在”和“不属于当前用户”：

```bash
A_THREAD_ID='<thread-id>'
A_BUNDLE_ID='<bundle-id>'
A_APPROVAL_ID='<approval-id>'
A_JOB_ID='<job-id>'

curl -o /tmp/e3-thread.json -w 'thread=%{http_code}\n' \
  -b "$COOKIE_B" "$PUBLIC_API/agent/threads/$A_THREAD_ID"

curl -o /tmp/e3-job.json -w 'job=%{http_code}\n' \
  -b "$COOKIE_B" "$PUBLIC_API/jobs/$A_JOB_ID"

curl -o /tmp/e3-bundle.json -w 'bundle=%{http_code}\n' \
  -b "$COOKIE_B" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: gate-e-bundle-$(date +%s)" \
  -d "{\"message\":\"test ownership\",\"input_bundle_id\":\"$A_BUNDLE_ID\",\"focus_job_ids\":[]}" \
  "$PUBLIC_API/agent/threads/$B_THREAD_ID/turns"

curl -o /tmp/e3-approval.json -w 'approval=%{http_code}\n' \
  -b "$COOKIE_B" \
  -H 'Content-Type: application/json' \
  -d '{"decision":"approve","plan_hash":"sha256:not-the-owner"}' \
  "$PUBLIC_API/agent/threads/$B_THREAD_ID/approvals/$A_APPROVAL_ID"
```

验收记录：2026-08-12 人工执行。首次复制命令时因 shell 续行中插入空行，A 资源变量为空，产生的 307/202 不计入验收；随后增加非空变量检查并使用生产 PostgreSQL 中用户 A 的真实资源 ID、全新用户 B Cookie 重新执行，最终结果为：

```text
thread=404
job=404
bundle=404
approval=404
```

四类响应均未返回资源归属信息。人工结论：通过。

## 4. 已完成：E4 模型/agent worker 故障不影响原业务

算力服务器停止 Copilot 专用组件，不停止 analysis worker：

```bash
cd /data/wb/omicsprism-worker/omicsprism-platform

sudo docker compose \
  -p omicsprism-agent \
  -f docker-compose.agent-worker.yml \
  stop agent-worker

sudo docker stop omicsprism-vllm
```

此时在云服务器和浏览器验证：

```bash
curl -fsS http://127.0.0.1:18086/health
curl -fsS http://127.0.0.1:18086/api/jobs -o /tmp/gate-e-jobs.json
```

人工执行一次原手工分析，并确认：

1. 手工表单可提交分析。
2. analysis worker 能从 Redis 取走任务并更新进度。
3. 结果页与已有结果文件下载正常。
4. Copilot turn 停留在 queued 或显示模型不可用，但 API、手工任务和结果页没有 5xx。

恢复：

```bash
sudo docker start omicsprism-vllm

until curl -fsS http://127.0.0.1:18000/health; do
  sleep 5
done

sudo docker compose \
  -p omicsprism-agent \
  -f docker-compose.agent-worker.yml \
  up -d agent-worker
```

验收记录：2026-08-13 人工执行。停止 vLLM 与 agent worker 期间没有停止 analysis worker；云服务器 API 保持可用：

```text
jobs_api=200
```

随后从原手工表单提交分析，analysis worker 正常取走任务并更新进度，任务成功完成，结果页与文件下载可用。Copilot 专用组件停机没有造成原 API、手工任务或结果页面 5xx。

恢复 vLLM 后，模型端点返回 `Qwen3-14B-AWQ`；恢复 agent worker 后容器状态为 `Up`：

```text
Qwen3-14B-AWQ, max_model_len=8192
omicsprism-agent-agent-worker-1  Up
```

人工结论：R6 通过。上述手工结论由验收人确认；报告不虚构未保存的 job id、耗时或下载文件名。

## 5. 自动化回归基线

当前最新本地结果：

```text
python -m pytest backend/tests -q -rs
158 passed, 6 skipped

python -m scripts.run_agent_eval --assembly unit
25 passed / 25 total

python -m compileall -q backend/app backend/agent_worker.py scripts
exit 0

npm test --prefix frontend
10 passed

npm run build --prefix frontend
build passed; main entry 206.06 kB, CopilotPage 26.62 kB
```

6 个 skip 均需要专用 PostgreSQL 测试环境变量；Gate A-C 已在服务器专用测试库执行真实权限/repository/API 用例。本报告不把 skip 记作通过。

## 6. Gate E DoD

- [x] 真实 DEG analyze → approve → job 完成。
- [x] 真实结果 interpretation → evidence → citation 完成。
- [x] thread/job/bundle/approval 跨用户请求全部 404，且响应不泄露归属。
- [x] 关闭 vLLM 与 agent worker 后，手工分析、进度、结果与下载继续正常。
- [x] agent worker compose 与无密钥环境模板纳入版本控制。
- [x] 四项人工演示全部通过后生成 `PHASE_6_REPORT.md`，并将根契约切回维护与回归。
