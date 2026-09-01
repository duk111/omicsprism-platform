# P0 Stage 0 Baseline

- Date: 2026-09-01
- Branch: `refactor/phase-4-persistence`
- Commit before this stage: `e4e3a4a`
- Purpose: freeze the observed architecture and establish reproducible quality
  measurements before P0 implementation work.

## Architecture baseline

- Cloud API persists Agent turns/messages and enqueues `AgentTurnWorkItem`.
- Compute-server `backend.agent_runtime` is the only production LangGraph/model/
  tool execution entrypoint.
- `backend.worker` consumes the independent analysis Job queue.
- Production Agent checkpoints are stored by `PostgresSaver` in PostgreSQL.
- `InMemorySaver` is reserved for tests and local fixtures.
- Redis delivery is at-least-once with a recoverable processing list.

See [`ADR_0001_AGENT_RUNTIME_BOUNDARY.md`](ADR_0001_AGENT_RUNTIME_BOUNDARY.md)
and [`AGENT_RUNTIME_DEPLOYMENT.md`](AGENT_RUNTIME_DEPLOYMENT.md) for the detailed
boundary and deployment contract.

## Reproducible commands

Run from the repository root in PowerShell:

```powershell
.venv\Scripts\python.exe -m pytest backend/tests -q
.venv\Scripts\python.exe scripts/run_agent_eval.py
npm --prefix frontend run test
npm --prefix frontend run build
npm --prefix frontend run test:e2e
docker compose -f docker-compose.yml -f docker-compose.expose.yml config
docker compose -f docker-compose.agent-runtime.yml config
```

The last two commands are syntax/topology checks only; they do not start
containers. Compose interpolation requires a local `.env` containing the
required deployment variables. Do not commit that file or paste its secrets
into this report.

## Results

Results from this stage are recorded below after running the commands in the
same checkout. A command that cannot run because a tool, browser, Docker daemon,
or required environment is unavailable is recorded explicitly rather than
replaced by a historical number.

| Check | Command | Result |
| --- | --- | --- |
| Backend tests | `\.venv\Scripts\python.exe -m pytest backend/tests -q` | **PASS**: 196 passed, 2 skipped, 2 deprecation warnings. The default Windows temp root first produced 5 permission errors; rerun with `TEMP`/`TMP` set to `.test-tmp` passed. |
| Agent eval | `\.venv\Scripts\python.exe scripts/run_agent_eval.py` | **PASS** overall. Parameter/ambiguity/capability gates passed; Grounded QA: citation validity 0.667, numeric consistency 0.667, unsupported-claim rate 0.333, hallucinated entity count 1. |
| Frontend unit tests | `npm --prefix frontend run test` | **PASS**: 4 files, 13 tests. |
| Frontend production build | `npm --prefix frontend run build` | **PASS**: 1850 modules transformed; build completed in 57.89s. Vite warns that the InteractiveRouter chunk is ~9.8 MB minified. |
| Frontend E2E | `npm --prefix frontend run test:e2e` | **PARTIAL**: 2 passed, 4 failed. Citation and model-outage assertions timed out; the Playwright Vite proxy also reported `ECONNREFUSED 127.0.0.1:8000` because no local API was running. |
| Cloud compose config | `docker compose -f docker-compose.yml -f docker-compose.expose.yml config` | **NOT RUN**: Docker CLI is not installed/available in this Windows environment. |
| Runtime compose config | `docker compose -f docker-compose.agent-runtime.yml config` | **NOT RUN**: Docker CLI is not installed/available in this Windows environment. |

## Known quality limits entering P0

- No full end-to-end test currently drives a real vLLM endpoint and a remote
  compute-server runtime.
- Multi-turn state/history merging and conversation summarization are not yet
  complete P0 capabilities.
- Structured assistant message blocks are not fully propagated through every
  response path.
- The Agent evaluation suite is smaller than the target 30+ scenario gate and
  still needs stronger negative/ambiguity coverage.
- Deployment verification depends on operator-provided cloud/compute network
  access and private environment variables.
