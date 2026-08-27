# v3 Agent Runtime Deployment

Stage 4C moves LangGraph execution out of the API process. The cloud API only
persists and publishes Agent turns to Redis. A runtime on the compute server
consumes those turns, uses the shared Postgres checkpointer, and calls the
local vLLM service.

## Cloud API

Keep the API and existing analysis services on the cloud server. Set the same
queue name in the cloud `.env` and in the compute-server environment:

```text
OMICS_PRISM_AGENT_QUEUE=omicsprism:agent-turns
```

Apply migrations before starting the API so the LangGraph checkpoint tables and
runtime role grants exist:

```bash
docker compose --profile migration run --rm migrate
docker compose up -d --build api housekeeping
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
  -f docker-compose.agent-runtime.yml \
  up -d --build agent-runtime
```

The cloud firewall must allow the compute server to reach Postgres `15432`,
Redis `16379`, and MinIO `19000`. vLLM remains bound to the compute host and
is not exposed to browsers.

## Delivery semantics

Agent work delivery is at-least-once. A processing-list entry is recovered
after a runtime crash. The graph checkpoint resumes from the last durable node;
analysis Job submission remains effectively-once through its idempotency key.
Do not run the removed legacy `agent-worker` container alongside this runtime.
