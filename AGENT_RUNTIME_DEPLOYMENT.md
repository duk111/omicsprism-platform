# v3 Agent Runtime Deployment

This is the current operational deployment contract for the v3 split runtime.
The historical document
[`docs/OMICS_PRISM_SERVER_DEPLOYMENT_MODE_FROZEN_ZH.md`](docs/OMICS_PRISM_SERVER_DEPLOYMENT_MODE_FROZEN_ZH.md)
is retained for audit history and is not an instruction to restore the old
API-local graph or legacy `agent-worker` topology. The architecture decision is
also captured in [`ADR_0001_AGENT_RUNTIME_BOUNDARY.md`](ADR_0001_AGENT_RUNTIME_BOUNDARY.md).

Stage 4C moves LangGraph execution out of the API process. The cloud API only
persists and publishes Agent turns to Redis. A runtime on the compute server
consumes those turns, uses the shared Postgres checkpointer, and calls the
local vLLM service.

## Cloud API

Keep the API and existing analysis services on the cloud server. Set the same
queue name in the cloud `.env` and in the compute-server environment:

```text
OMICS_PRISM_AGENT_QUEUE=omicsprism:agent-turns
JOB_TIMEOUT_SECONDS=7200
```

Apply migrations before starting the API so the LangGraph checkpoint tables and
runtime role grants exist:

```bash
docker compose --env-file .env -p omicsprism \
  --profile migration run --rm migrate
docker compose --env-file .env -p omicsprism \
  up -d --build api housekeeping
```

The API and runtime use the same graph/checkpointer contract, but only the
runtime executes graph turns and makes model requests.

## Compute server

The runtime uses host networking so `127.0.0.1:18000` addresses vLLM on the
compute host, not the runtime container. Copy the repository version matching
the cloud API and create a private environment file containing:

```text
OMICS_PRISM_STORAGE_BACKEND=postgres
OMICS_PRISM_RUNTIME_DATABASE_URL=postgresql://omics_app:<password>@<cloud-host>:15432/omicsprism
OMICS_PRISM_EXECUTOR=redis
OMICS_PRISM_REDIS_URL=redis://<cloud-host>:16379/0
OMICS_PRISM_AGENT_QUEUE=omicsprism:agent-turns
OMICS_PRISM_AGENT_MODEL_URL=http://127.0.0.1:18000/v1
OMICS_PRISM_AGENT_MODEL_NAME=<served-model-name>
OMICS_PRISM_FILE_STORAGE_BACKEND=s3
OMICS_PRISM_S3_ENDPOINT_URL=http://<cloud-host>:19000
OMICS_PRISM_S3_ACCESS_KEY_ID=<minio-user>
OMICS_PRISM_S3_SECRET_ACCESS_KEY=<minio-password>
```

Start the runtime with the dedicated overlay:

```bash
docker compose --env-file .env \
  -p omicsprism \
  -f docker-compose.agent-runtime.yml \
  up -d --build agent-runtime
```

The cloud firewall must allow the compute server to reach Postgres `15432`,
Redis `16379`, and MinIO `19000`. vLLM remains bound to the compute host and
is not exposed to browsers.

`JOB_TIMEOUT_SECONDS` is read by the analysis worker. A queued or running Job
whose age exceeds this limit is atomically marked `failed` with the explicit
`job_timeout` error, and the normal completion outbox then wakes any Agent wait.
Set this to a value appropriate for the largest expected analysis; changing it
requires restarting the analysis worker.

## Delivery semantics

Agent work delivery is at-least-once. A processing-list entry is recovered
after a runtime crash. The graph checkpoint resumes from the last durable node;
analysis Job submission remains effectively-once through its idempotency key.
Do not run the removed legacy `agent-worker` container alongside this runtime.

## Verification

Run these checks after every coordinated deployment. Values shown by `inspect`
should be reviewed without exposing passwords or API keys:

```bash
# Cloud server
docker compose --env-file .env -p omicsprism ps
curl -fsS http://127.0.0.1:18086/health
docker compose --env-file .env -p omicsprism exec -T redis \
  redis-cli LLEN omicsprism:agent-turns

# Compute server
docker compose --env-file .env -p omicsprism \
  -f docker-compose.agent-runtime.yml ps
docker compose --env-file .env -p omicsprism \
  -f docker-compose.agent-runtime.yml logs --tail 100 agent-runtime
curl -fsS http://127.0.0.1:18000/health
curl -fsS http://127.0.0.1:18000/v1/models
```

Submit one short Agent turn and confirm all of the following:

1. The API returns `202` and the cloud Redis Agent queue briefly contains the
   work item.
2. The runtime log contains a model request and `agent.turn.processed` (or a
   clear `agent.turn.failed` event).
3. The corresponding `agent_turns` row reaches `completed`, `failed`, or an
   explicit HITL-waiting state, and an assistant message exists for terminal
   turns.

If the queue stays at zero immediately after a `202`, check that API and runtime
use the same `OMICS_PRISM_AGENT_QUEUE` and Redis database. If the queue grows,
check runtime connectivity to PostgreSQL, Redis, MinIO, and local vLLM. A model
HTTP 200 does not by itself prove a valid Agent response; inspect runtime logs
for boundary-validation errors and the turn row for its terminal error code.

For a timeout drill, temporarily set `JOB_TIMEOUT_SECONDS` to a small value in
the compute-server environment, restart `omicsprism-worker-1`, and verify the
Job row has `status=failed`, `error=job_timeout`; then restore the production
value and restart the worker again.
