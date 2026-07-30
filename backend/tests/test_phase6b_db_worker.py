from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest


ADMIN_DSN = os.getenv("OMICS_PRISM_TEST_DATABASE_URL")
APP_DSN = os.getenv("OMICS_PRISM_TEST_APP_DATABASE_URL")
APP_PASSWORD = os.getenv("OMICS_PRISM_APP_DB_PASSWORD")
HAS_TEST_DATABASE = bool(ADMIN_DSN and APP_DSN and APP_PASSWORD)


@pytest.mark.skipif(
    not HAS_TEST_DATABASE,
    reason="需要专用 PostgreSQL 测试库和 OMICS_PRISM_TEST_* 环境变量",
)
def test_postgres_worker_claim_lease_recovery_and_global_serial_slot() -> None:
    import psycopg
    from psycopg.types.json import Jsonb

    from backend.app.agent.product_store import PostgresAgentProductStore
    from backend.app.agent.schemas import AgentEvent, AgentTextBlock, AgentThreadRecord, AgentTurnRecord
    from backend.app.agent.store import PostgresStateStore
    from scripts.migrate import apply_migrations

    assert ADMIN_DSN and APP_DSN and APP_PASSWORD
    apply_migrations(ADMIN_DSN, APP_PASSWORD)
    suffix = str(uuid4())
    user_id = f"user-{suffix}"
    thread_id = f"thread-{suffix}"
    run_id = f"run-{suffix}"
    turn_id = f"turn-{suffix}"
    now = datetime.now(timezone.utc)
    store = PostgresAgentProductStore(APP_DSN)

    try:
        with psycopg.connect(APP_DSN) as conn:
            conn.execute(
                """
                insert into agent_runs (
                    run_id, user_id, thread_id, active_profile, state, step_no,
                    focus, model_calls, tool_calls, status, version
                ) values (%s, %s, %s, 'analysis', 'CHECK_INPUTS', 0, %s, 0, 0, 'running', 0)
                """,
                (run_id, user_id, thread_id, Jsonb({
                    "in_scope_job_ids": [], "resolved_entities": {}, "last_citation": None,
                })),
            )
        store.save_thread(AgentThreadRecord(
            thread_id=thread_id,
            user_id=user_id,
            title="lease test",
            current_run_id=run_id,
            status="active",
            version=0,
            created_at=now,
            updated_at=now,
        ))
        store.create_turn(AgentTurnRecord(
            turn_id=turn_id,
            thread_id=thread_id,
            run_id=run_id,
            user_id=user_id,
            idempotency_key=f"key-{suffix}",
            request_hash="sha256:lease",
            status="queued",
            attempt=0,
            lease_owner=None,
            lease_expires_at=None,
            error_code=None,
            created_at=now,
            updated_at=now,
            started_at=None,
            completed_at=None,
        ))

        first = store.claim_next_turn(worker_id="worker-a", now=now, lease_seconds=10)
        assert first is not None and first.attempt == 1
        assert store.claim_next_turn(
            worker_id="worker-b", now=now + timedelta(seconds=5), lease_seconds=10,
        ) is None
        recovered = store.claim_next_turn(
            worker_id="worker-b", now=now + timedelta(seconds=11), lease_seconds=10,
        )
        assert recovered is not None and recovered.turn_id == turn_id and recovered.attempt == 2
        state = PostgresStateStore(APP_DSN).get(run_id=run_id, user_id=user_id)
        state.step_no = 1
        completed = store.commit_turn_result(
            turn=recovered,
            worker_id="worker-b",
            state=state.model_copy(update={"version": 1}),
            expected_version=0,
            blocks=[AgentTextBlock(text="completed")],
            events=[AgentEvent(
                event_id=f"event-{suffix}",
                run_id=run_id,
                user_id=user_id,
                step_no=1,
                event_type="turn.completed",
                payload={"turn_id": turn_id},
            )],
            now=now + timedelta(seconds=12),
        )
        assert completed.status.value == "completed"
        with psycopg.connect(APP_DSN) as conn:
            assert conn.execute(
                "select version from agent_runs where run_id = %s and user_id = %s",
                (run_id, user_id),
            ).fetchone() == (1,)
            assert conn.execute(
                "select count(*) from agent_messages where message_id = %s",
                (f"assistant-{turn_id}",),
            ).fetchone() == (1,)
            assert conn.execute(
                "select count(*) from agent_events where event_id = %s",
                (f"event-{suffix}",),
            ).fetchone() == (1,)

        second_store = PostgresAgentProductStore(APP_DSN)
        with store.worker_slot() as first_slot:
            assert first_slot
            with second_store.worker_slot() as second_slot:
                assert not second_slot
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
            conn.execute("delete from agent_events where event_id = %s", (f"event-{suffix}",))
            conn.execute("delete from agent_messages where message_id = %s", (f"assistant-{turn_id}",))
            conn.execute("delete from agent_turns where turn_id = %s", (turn_id,))
            conn.execute("delete from agent_threads where thread_id = %s", (thread_id,))
            conn.execute("delete from agent_runs where run_id = %s and user_id = %s", (run_id, user_id))


@pytest.mark.skipif(
    not HAS_TEST_DATABASE,
    reason="需要专用 PostgreSQL 测试库和 OMICS_PRISM_TEST_* 环境变量",
)
def test_postgres_repositories_complete_approved_analysis_turns_atomically() -> None:
    import psycopg
    from psycopg.types.json import Jsonb

    from backend.app.agent.approvals import PostgresApprovalGate
    from backend.app.agent.audit import PostgresAgentEventStore
    from backend.app.agent.model import ScriptedModelAdapter
    from backend.app.agent.plans import PostgresPlanStore
    from backend.app.agent.product_store import PostgresAgentProductStore
    from backend.app.agent.runtime import ProductionRunCoordinator
    from backend.app.agent.schemas import (
        ActiveProfile,
        AgentAction,
        AgentDecision,
        AgentThreadRecord,
        AgentTurnRecord,
        Feasibility,
        FeasibilityVerdict,
    )
    from backend.app.agent.store import PostgresStateStore
    from backend.app.agent.tools import AgentInputFile, AgentToolRuntime
    from backend.app.models import AnalysisType, JobRecord, JobStatus
    from scripts.migrate import apply_migrations

    assert ADMIN_DSN and APP_DSN and APP_PASSWORD
    apply_migrations(ADMIN_DSN, APP_PASSWORD)
    suffix = str(uuid4())
    user_id = f"user-{suffix}"
    thread_id = f"thread-{suffix}"
    run_id = f"run-{suffix}"
    now = datetime.now(timezone.utc)
    product_store = PostgresAgentProductStore(APP_DSN)
    state_store = PostgresStateStore(APP_DSN)
    plans = PostgresPlanStore(APP_DSN)
    approvals = PostgresApprovalGate(APP_DSN)

    class _Jobs:
        def __init__(self) -> None:
            self.source = JobRecord(
                id=f"source-{suffix}",
                project_name="source",
                analysis_type=AnalysisType.DIFFERENTIAL,
                status=JobStatus.SUCCEEDED,
                created_at=now,
                updated_at=now,
                owner_id=user_id,
            )
            self.saved: list[JobRecord] = []

        def get_for_user(self, job_id: str, owner_id: str) -> JobRecord:
            if owner_id != user_id:
                raise LookupError(job_id)
            if job_id == self.source.id:
                return self.source
            match = next((item for item in self.saved if item.id == job_id), None)
            if match is None:
                raise LookupError(job_id)
            return match

        def save(self, job: JobRecord) -> None:
            self.saved.append(job)

    class _Files:
        def copy_input_artifact(self, _source_job_id, _target_job_id, source):
            return source

    class _Executor:
        def __init__(self) -> None:
            self.enqueued: list[str] = []

        def enqueue(self, job_id: str) -> None:
            self.enqueued.append(job_id)

    jobs = _Jobs()
    executor = _Executor()
    runtime = AgentToolRuntime(
        user_id=user_id,
        inputs={
            "counts": AgentInputFile("counts.csv", b"gene,s1,s2,s3,s4\ng1,10,12,30,32\n"),
            "metadata": AgentInputFile(
                "metadata.csv",
                b"sample_id,treatment,batch\n"
                b"s1,control,b1\n"
                b"s2,control,b1\n"
                b"s3,salt,b1\n"
                b"s4,salt,b1\n",
            ),
        },
        input_source_job_id=jobs.source.id,
        plans=plans,
        job_store=jobs,
        files=_Files(),
        executor=executor,
        approval_gate=approvals,
    )
    model = ScriptedModelAdapter([AgentDecision(
        action=AgentAction.PROPOSE_PLAN,
        reasoning_summary="输入可进行差异分析",
        feasibility=Feasibility(
            verdict=FeasibilityVerdict.ANSWERABLE,
            reasons=["counts and metadata are present"],
            missing_information=[],
        ),
        analysis_recommendations=[AnalysisType.DIFFERENTIAL],
        requires_approval=True,
        requested_params={
            "compare_field": "treatment",
            "tested_levels": "salt",
            "reference_level": "control",
            "same_fields": "batch",
            "min_replicates": 2,
        },
        grounded_answer=None,
    )])

    def running_turn(name: str, idempotency_key: str) -> AgentTurnRecord:
        return AgentTurnRecord(
            turn_id=f"turn-{name}-{suffix}",
            thread_id=thread_id,
            run_id=run_id,
            user_id=user_id,
            idempotency_key=idempotency_key,
            request_hash=f"sha256:{name}",
            status="running",
            attempt=1,
            lease_owner="worker-db",
            lease_expires_at=now + timedelta(minutes=2),
            error_code=None,
            created_at=now,
            updated_at=now,
            started_at=now,
            completed_at=None,
        )

    try:
        with psycopg.connect(APP_DSN) as conn:
            conn.execute(
                """
                insert into agent_runs (
                    run_id, user_id, thread_id, active_profile, state, step_no,
                    focus, model_calls, tool_calls, status, version
                ) values (%s, %s, %s, 'analysis', 'CHECK_INPUTS', 0, %s, 0, 0, 'running', 0)
                """,
                (run_id, user_id, thread_id, Jsonb({
                    "in_scope_job_ids": [], "resolved_entities": {}, "last_citation": None,
                })),
            )
        product_store.save_thread(AgentThreadRecord(
            thread_id=thread_id,
            user_id=user_id,
            title="real repository analysis",
            current_run_id=run_id,
            status="active",
            version=0,
            created_at=now,
            updated_at=now,
        ))
        proposal_turn = running_turn("proposal", f"proposal-{suffix}")
        product_store.create_turn(proposal_turn)
        coordinator = ProductionRunCoordinator(
            state_store=state_store,
            plan_store=plans,
            approval_gate=approvals,
            event_store=PostgresAgentEventStore(APP_DSN),
            model=model,
            tool_runtime=runtime,
        )
        proposal = coordinator.execute_turn(
            turn=proposal_turn,
            user_message="比较 salt 与 control",
            persist=False,
        )
        product_store.commit_turn_result(
            turn=proposal_turn,
            worker_id="worker-db",
            state=proposal.state,
            expected_version=proposal.expected_version,
            blocks=proposal.blocks,
            events=proposal.events,
            now=now,
        )

        assert jobs.saved == []
        assert executor.enqueued == []
        assert proposal.state.pending_approval_id
        approvals.resume(
            approval_id=proposal.state.pending_approval_id,
            run_id=run_id,
            user_id=user_id,
            plan_hash=proposal.state.plan_hash or "",
            now=now,
        )

        submit_turn = running_turn("submit", f"submit-{suffix}")
        product_store.create_turn(submit_turn)
        submitted = coordinator.execute_turn(turn=submit_turn, user_message="", persist=False)
        product_store.commit_turn_result(
            turn=submit_turn,
            worker_id="worker-db",
            state=submitted.state,
            expected_version=submitted.expected_version,
            blocks=submitted.blocks,
            events=submitted.events,
            now=now + timedelta(seconds=1),
        )

        assert [block.type for block in submitted.blocks] == ["job"]
        assert len(jobs.saved) == 1
        assert len(executor.enqueued) == 1
        with psycopg.connect(APP_DSN) as conn:
            assert conn.execute(
                "select count(*) from agent_messages where run_id = %s and user_id = %s",
                (run_id, user_id),
            ).fetchone() == (2,)
            assert conn.execute(
                "select count(*) from agent_events where run_id = %s and user_id = %s",
                (run_id, user_id),
            ).fetchone() == (2,)
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
            conn.execute("delete from agent_events where run_id = %s and user_id = %s", (run_id, user_id))
            conn.execute("delete from agent_messages where run_id = %s and user_id = %s", (run_id, user_id))
            conn.execute("delete from agent_turns where run_id = %s and user_id = %s", (run_id, user_id))
            conn.execute("delete from agent_approvals where run_id = %s and user_id = %s", (run_id, user_id))
            conn.execute("delete from agent_plans where run_id = %s and user_id = %s", (run_id, user_id))
            conn.execute("delete from agent_threads where thread_id = %s and user_id = %s", (thread_id, user_id))
            conn.execute("delete from agent_runs where run_id = %s and user_id = %s", (run_id, user_id))
