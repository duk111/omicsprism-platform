from __future__ import annotations

from backend.app.agent.bootstrap import create_agent_api_context
from backend.app.agent.runtime import AgentRuntime
from backend.app.bootstrap import create_context
from backend.app.observability import configure_logging
from backend.app.settings import load_settings


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    if settings.executor_backend != "redis":
        raise SystemExit("Agent runtime requires OMICS_PRISM_EXECUTOR=redis")
    context = create_context(
        settings,
        ensure_figure_specs=lambda _job_id: {},
        remaining_seconds=lambda _job, _progress: None,
        include_executor=True,
    )
    if context.job_executor is None:
        raise SystemExit("Agent runtime requires a job executor")
    agent_context = create_agent_api_context(
        settings,
        files=context.files,
        job_store=context.job_store,
        job_executor=context.job_executor,
    )
    if agent_context is None:
        raise SystemExit("Agent runtime requires PostgreSQL Agent persistence")
    if agent_context.turn_queue is None:
        raise SystemExit("Agent runtime queue is not configured")
    queue = agent_context.turn_queue
    try:
        AgentRuntime(
            agent_context,
            queue,
            turn_timeout_seconds=settings.agent_turn_timeout_seconds,
            max_transient_retries=settings.agent_max_transient_retries,
            retry_base_seconds=settings.agent_retry_base_seconds,
            retry_max_seconds=settings.agent_retry_max_seconds,
            retry_jitter_seconds=settings.agent_retry_jitter_seconds,
        ).run_forever()
    finally:
        agent_context.close()


if __name__ == "__main__":
    main()
