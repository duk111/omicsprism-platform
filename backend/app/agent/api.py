from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Annotated, Callable
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from ..observability import LOG
from ..storage_service import AGENT_BUNDLE_MAX_BYTES
from .bootstrap import AgentApiContext
from .dataset_profile import build_dataset_profiles
from .graph import (
    DatasetLoadRequest,
    DatasetProfileRef,
    GraphClarificationResumeRequest,
    GraphConfirmationResumeRequest,
    GraphInterrupt,
    GraphPendingInterrupt,
    GraphResumeRequest,
    GraphState,
    AgentStreamEvent,
    GraphTurnResult,
    JobRef,
    ConfirmationPayload,
)
from .product_store import ActiveTurnConflict, AgentResourceNotFound, IdempotencyConflict, TurnConflict
from .queue import AgentTurnWorkItem
from .schemas import (
    AgentInputBundleRecord,
    AgentInputBundleResponse,
    AgentInputFileRecord,
    AgentInputFileResponse,
    AgentInputSummaryBlock,
    AgentMessageListResponse,
    AgentMessageRecord,
    AgentMessageResponse,
    AgentMessageRole,
    AgentRunResponse,
    AgentTextBlock,
    AgentThreadCreateRequest,
    AgentThreadDetailResponse,
    AgentThreadListResponse,
    AgentThreadRecord,
    AgentThreadResponse,
    AgentTurnCreateRequest,
    AgentTurnListResponse,
    AgentTurnRecord,
    AgentTurnResponse,
    AgentTurnStatus,
    RunFocus,
)


ALLOWED_INPUT_FIELDS = {"counts", "metadata", "metabs", "transcriptome", "metabolome", "group"}


def create_agent_router(
    *,
    context: AgentApiContext | None,
    session_dependency: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api/agent", tags=["agent"])

    def current_context() -> AgentApiContext:
        if context is None:
            raise HTTPException(status_code=503, detail="Agent persistence is not configured")
        return context

    @router.post("/threads", response_model=AgentThreadResponse, status_code=201)
    def create_thread(
        payload: AgentThreadCreateRequest,
        user_id: str = Depends(session_dependency),
        ctx: AgentApiContext = Depends(current_context),
    ) -> AgentThreadResponse:
        _require_owned_jobs(ctx, payload.focus_job_ids, user_id)
        now = datetime.now(timezone.utc)
        thread_id = f"thread-{uuid4()}"
        run_id = f"run-{uuid4()}"
        focus = RunFocus(
            in_scope_job_ids=list(payload.focus_job_ids),
            resolved_entities={},
            last_citation=None,
        )
        thread = AgentThreadRecord(
            thread_id=thread_id,
            user_id=user_id,
            title="New Copilot thread",
            current_run_id=run_id,
            status="active",
            version=0,
            created_at=now,
            updated_at=now,
        )
        try:
            ctx.product_store.save_thread(thread)
            _initialize_graph_checkpoint(ctx, thread_id, user_id, focus)
        except Exception as exc:
            raise _conflict("Agent thread could not be created") from exc
        return _thread_response(thread)

    @router.get("/threads", response_model=AgentThreadListResponse)
    def list_threads(
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        after: str | None = None,
        user_id: str = Depends(session_dependency),
        ctx: AgentApiContext = Depends(current_context),
    ) -> AgentThreadListResponse:
        records = ctx.product_store.list_threads(user_id=user_id, limit=100)
        page = _after_id(records, after, "thread_id")[:limit]
        return AgentThreadListResponse(
            threads=[_thread_response(item) for item in page],
            next_cursor=page[-1].thread_id if len(records) > limit and page else None,
        )

    @router.get("/threads/{thread_id}", response_model=AgentThreadDetailResponse)
    def get_thread(
        thread_id: str,
        user_id: str = Depends(session_dependency),
        ctx: AgentApiContext = Depends(current_context),
    ) -> AgentThreadDetailResponse:
        thread = _owned_thread(ctx, thread_id, user_id)
        focus, version = _owned_graph_focus(ctx, thread_id, user_id)
        return AgentThreadDetailResponse(
            thread=_thread_response(thread),
            run=_run_response(thread.current_run_id, thread_id, focus, version),
        )

    @router.get(
        "/threads/{thread_id}/pending-interrupt",
        response_model=GraphPendingInterrupt | None,
    )
    def get_pending_interrupt(
        thread_id: str,
        user_id: str = Depends(session_dependency),
        ctx: AgentApiContext = Depends(current_context),
    ) -> GraphPendingInterrupt | None:
        _owned_thread(ctx, thread_id, user_id)
        return _pending_graph_interrupt(ctx, thread_id, user_id)

    @router.delete("/threads/{thread_id}", status_code=204)
    def delete_thread(
        thread_id: str,
        user_id: str = Depends(session_dependency),
        ctx: AgentApiContext = Depends(current_context),
    ) -> None:
        _owned_thread(ctx, thread_id, user_id)
        active_turns = [
            item for item in ctx.product_store.list_turns(
                thread_id=thread_id, user_id=user_id, limit=100
            ) if item.status in {AgentTurnStatus.QUEUED, AgentTurnStatus.RUNNING}
        ]
        if active_turns:
            raise _conflict("Stop the active request before deleting this conversation")
        try:
            _delete_graph_checkpoint(ctx, thread_id)
            files = ctx.product_store.delete_thread(thread_id=thread_id, user_id=user_id)
            if ctx.files is not None:
                for item in files:
                    try:
                        ctx.files.delete_staged_upload(item.storage_key)
                    except Exception:
                        LOG.warning("agent input file cleanup failed", extra={"thread_id": thread_id, "storage_key": item.storage_key})
        except AgentResourceNotFound as exc:
            raise _not_found() from exc
        except HTTPException:
            raise
        except Exception as exc:
            raise _conflict("Agent thread could not be deleted") from exc

    @router.get("/threads/{thread_id}/messages", response_model=AgentMessageListResponse)
    def list_messages(
        thread_id: str,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
        after: str | None = None,
        user_id: str = Depends(session_dependency),
        ctx: AgentApiContext = Depends(current_context),
    ) -> AgentMessageListResponse:
        _owned_thread(ctx, thread_id, user_id)
        records = ctx.product_store.list_messages(thread_id=thread_id, user_id=user_id, limit=100)
        page = _after_id(records, after, "message_id")[:limit]
        return AgentMessageListResponse(
            messages=[_message_response(item) for item in page],
            next_cursor=page[-1].message_id if len(records) > limit and page else None,
        )

    @router.get("/threads/{thread_id}/turns", response_model=AgentTurnListResponse)
    def list_turns(
        thread_id: str,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
        after: str | None = None,
        user_id: str = Depends(session_dependency),
        ctx: AgentApiContext = Depends(current_context),
    ) -> AgentTurnListResponse:
        _owned_thread(ctx, thread_id, user_id)
        records = ctx.product_store.list_turns(thread_id=thread_id, user_id=user_id, limit=100)
        page = _after_id(records, after, "turn_id")[:limit]
        return AgentTurnListResponse(
            turns=[_turn_response(item) for item in page],
            next_cursor=page[-1].turn_id if len(records) > limit and page else None,
        )

    @router.post(
        "/threads/{thread_id}/input-bundles",
        response_model=AgentInputBundleResponse,
        status_code=201,
    )
    async def create_input_bundle(
        thread_id: str,
        request: Request,
        files: Annotated[list[UploadFile], File()],
        fields: Annotated[list[str], Form()],
        user_id: str = Depends(session_dependency),
        ctx: AgentApiContext = Depends(current_context),
    ) -> AgentInputBundleResponse:
        _owned_thread(ctx, thread_id, user_id)
        if ctx.files is None:
            raise HTTPException(status_code=503, detail="Agent file storage is not configured")
        form = await request.form()
        if set(form.keys()) - {"files", "fields"}:
            raise HTTPException(status_code=422, detail="Unexpected multipart field")
        if not files or len(files) != len(fields) or len(files) > 6:
            raise HTTPException(status_code=400, detail="Provide one to six files with matching field roles")
        if any(field not in ALLOWED_INPUT_FIELDS for field in fields) or len(set(fields)) != len(fields):
            raise HTTPException(status_code=400, detail="Input field roles are invalid or duplicated")

        now = datetime.now(timezone.utc)
        bundle = AgentInputBundleRecord(
            bundle_id=f"bundle-{uuid4()}",
            thread_id=thread_id,
            user_id=user_id,
            status="active",
            expires_at=now + timedelta(hours=24),
            created_at=now,
        )
        records: list[AgentInputFileRecord] = []
        total_size = 0
        new_fields = set(fields)
        try:
            # 查找同一 thread 内最近的 active bundle，继承未覆盖的文件
            previous_bundle = ctx.product_store.get_latest_active_bundle(
                thread_id=thread_id,
                user_id=user_id,
                before=now,
            )
            if previous_bundle is not None:
                previous_files = ctx.product_store.list_input_files(
                    bundle_id=previous_bundle.bundle_id,
                    user_id=user_id,
                )
                for prev_file in previous_files:
                    if prev_file.field not in new_fields:
                        # 继承未被新上传覆盖的角色
                        inherited = AgentInputFileRecord(
                            file_id=f"file-{uuid4()}",
                            bundle_id=bundle.bundle_id,
                            user_id=user_id,
                            field=prev_file.field,
                            filename=prev_file.filename,
                            storage_key=prev_file.storage_key,
                            checksum=prev_file.checksum,
                            content_type=prev_file.content_type,
                            size_bytes=prev_file.size_bytes,
                            created_at=now,
                        )
                        records.append(inherited)
                        total_size += inherited.size_bytes

            for field, upload in zip(fields, files):
                stored = await ctx.files.save_staged_upload(bundle.bundle_id, field, upload)
                total_size += stored.size_bytes
                records.append(AgentInputFileRecord(
                    file_id=f"file-{uuid4()}",
                    bundle_id=bundle.bundle_id,
                    user_id=user_id,
                    field=field,
                    filename=stored.filename,
                    storage_key=stored.storage_key,
                    checksum=stored.checksum,
                    content_type=stored.content_type,
                    size_bytes=stored.size_bytes,
                    created_at=stored.created_at,
                ))
                if total_size > AGENT_BUNDLE_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="Input bundle exceeds 150 MB limit")
            ctx.product_store.save_input_bundle_with_files(bundle=bundle, files=records)
        except Exception:
            for item in records:
                ctx.files.delete_staged_upload(item.storage_key)
            raise
        return _bundle_response(bundle, records)

    @router.post(
        "/threads/{thread_id}/turns",
        response_model=GraphTurnResult,
        status_code=202,
    )
    def create_turn(
        thread_id: str,
        payload: AgentTurnCreateRequest,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)],
        user_id: str = Depends(session_dependency),
        ctx: AgentApiContext = Depends(current_context),
    ) -> GraphTurnResult:
        thread = _owned_thread(ctx, thread_id, user_id)
        focus, version = _owned_graph_focus(ctx, thread_id, user_id)
        _require_owned_jobs(ctx, payload.focus_job_ids, user_id)
        blocks = [AgentTextBlock(text=payload.message)]
        input_files: list[AgentInputFileRecord] = []
        if payload.input_bundle_id:
            bundle, input_files = _owned_active_bundle(ctx, payload.input_bundle_id, thread_id, user_id)
            blocks.append(AgentInputSummaryBlock(
                bundle_id=bundle.bundle_id,
                files=[_file_response(item) for item in input_files],
            ))
        focus_job_ids = payload.focus_job_ids or focus.in_scope_job_ids
        _require_owned_jobs(ctx, focus_job_ids, user_id)
        if payload.focus_job_ids:
            focus = focus.model_copy(update={"in_scope_job_ids": list(payload.focus_job_ids)})
            version += 1
        turn_id = f"turn-{uuid4()}"
        trace_id = f"trace-{uuid4()}"
        graph_state = GraphState(
            thread_id=thread_id,
            user_id=user_id,
            trace_id=trace_id,
            turn_id=turn_id,
            run_id=thread.current_run_id,
            user_message=payload.message,
            focus=focus,
            version=version,
            dataset_profiles=_graph_dataset_profiles(ctx, input_files, user_id),
            recent_jobs=[
                JobRef(job_id=job_id, owner_id=user_id)
                for job_id in focus_job_ids
            ],
        )
        now = datetime.now(timezone.utc)
        turn = AgentTurnRecord(
            turn_id=turn_id,
            thread_id=thread_id,
            run_id=thread.current_run_id,
            user_id=user_id,
            trace_id=trace_id,
            idempotency_key=idempotency_key,
            request_hash=_request_hash(thread_id, payload.model_dump(mode="json")),
            status=AgentTurnStatus.QUEUED,
            attempt=0,
            error_code=None,
            created_at=now,
            updated_at=now,
            started_at=None,
            completed_at=None,
        )
        message = AgentMessageRecord(
            message_id=f"user-{turn_id}",
            thread_id=thread_id,
            run_id=thread.current_run_id,
            trace_id=trace_id,
            user_id=user_id,
            role=AgentMessageRole.USER,
            blocks=blocks,
            created_at=now,
        )
        try:
            queued, created = ctx.product_store.enqueue_turn(
                message=message,
                turn=turn,
            )
        except (IdempotencyConflict, ActiveTurnConflict) as exc:
            raise _conflict(str(exc) or "Agent turn conflicts with current state") from exc
        if not created:
            if queued.status is AgentTurnStatus.QUEUED:
                _enqueue_work(ctx, AgentTurnWorkItem(
                    turn_id=queued.turn_id,
                    thread_id=queued.thread_id,
                    trace_id=queued.trace_id,
                    user_id=queued.user_id,
                    state=graph_state.model_copy(update={
                        "trace_id": queued.trace_id,
                        "turn_id": queued.turn_id,
                    }),
                ))
            return _queued_graph_turn_result(queued)
        _enqueue_work(ctx, AgentTurnWorkItem(
            turn_id=turn_id,
            thread_id=thread_id,
            trace_id=trace_id,
            user_id=user_id,
            state=graph_state,
        ))
        if ctx.trace_recorder is not None:
            ctx.trace_recorder.turn_event(
                event_type="turn.queued",
                trace_id=trace_id,
                thread_id=thread_id,
                turn_id=turn_id,
                run_id=thread.current_run_id,
                user_id=user_id,
                outcome="queued",
            )
        return _queued_graph_turn_result(queued)

    @router.post(
        "/threads/{thread_id}/turns/{checkpoint_turn_id}/resume",
        response_model=GraphTurnResult,
    )
    def resume_graph_turn(
        thread_id: str,
        checkpoint_turn_id: str,
        payload: GraphResumeRequest,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", min_length=1, max_length=200),
        ] = None,
        user_id: str = Depends(session_dependency),
        ctx: AgentApiContext = Depends(current_context),
    ) -> GraphTurnResult:
        thread = _owned_thread(ctx, thread_id, user_id)
        try:
            turn = ctx.product_store.get_turn(turn_id=checkpoint_turn_id, user_id=user_id)
        except AgentResourceNotFound as exc:
            raise _not_found() from exc
        if turn.thread_id != thread_id:
            raise _not_found()
        config = _graph_config(thread.thread_id)
        try:
            snapshot, _ = _owned_graph_snapshot(ctx, config, thread_id, user_id)
        except HTTPException:
            raise
        interrupts = _snapshot_interrupts(snapshot)
        if len(interrupts) != 1 or interrupts[0].interrupt_id != payload.interrupt_id:
            raise _conflict("Interrupt is stale or does not match the current graph state")
        interrupt_item = interrupts[0]
        if interrupt_item.payload.kind != payload.kind:
            raise _conflict("Resume payload does not match the pending interrupt")
        if isinstance(payload, GraphConfirmationResumeRequest):
            current_payload = interrupt_item.payload
            if not isinstance(current_payload, ConfirmationPayload):
                raise _conflict("Resume payload does not match the pending interrupt")
            if (
                payload.plan_id != current_payload.plan_id
                or payload.plan_version != current_payload.plan_version
            ):
                raise _conflict(
                    "Pending analysis plan is stale; review the current plan before resuming"
                )
        if isinstance(payload, GraphConfirmationResumeRequest) and payload.approve is True:
            if idempotency_key is None:
                raise HTTPException(
                    status_code=422,
                    detail="Idempotency-Key is required to run an analysis",
                )
        if turn.status not in {AgentTurnStatus.RUNNING, AgentTurnStatus.QUEUED}:
            raise _conflict("Agent graph turn is no longer awaiting input")
        try:
            queued = (
                ctx.product_store.queue_turn(
                    turn_id=turn.turn_id,
                    user_id=user_id,
                    now=datetime.now(timezone.utc),
                )
                if turn.status is AgentTurnStatus.RUNNING
                else turn
            )
            _enqueue_work(ctx, AgentTurnWorkItem(
                turn_id=turn.turn_id,
                thread_id=thread_id,
                trace_id=turn.trace_id,
                user_id=user_id,
                resume=payload,
                idempotency_key=idempotency_key,
            ))
        except TurnConflict as exc:
            raise _conflict("Agent graph turn is no longer awaiting input") from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Agent turn could not be queued") from exc
        return _queued_graph_turn_result(queued)

    @router.get("/threads/{thread_id}/turns/{turn_id}", response_model=AgentTurnResponse)
    def get_turn(
        thread_id: str,
        turn_id: str,
        user_id: str = Depends(session_dependency),
        ctx: AgentApiContext = Depends(current_context),
    ) -> AgentTurnResponse:
        _owned_thread(ctx, thread_id, user_id)
        try:
            turn = ctx.product_store.get_turn(turn_id=turn_id, user_id=user_id)
        except AgentResourceNotFound as exc:
            raise _not_found() from exc
        if turn.thread_id != thread_id:
            raise _not_found()
        return _turn_response(turn)

    @router.post("/threads/{thread_id}/turns/{turn_id}/cancel", response_model=AgentTurnResponse)
    def cancel_turn(
        thread_id: str,
        turn_id: str,
        user_id: str = Depends(session_dependency),
        ctx: AgentApiContext = Depends(current_context),
    ) -> AgentTurnResponse:
        _owned_thread(ctx, thread_id, user_id)
        try:
            turn = ctx.product_store.get_turn(turn_id=turn_id, user_id=user_id)
        except AgentResourceNotFound as exc:
            raise _not_found() from exc
        if turn.thread_id != thread_id:
            raise _not_found()
        try:
            cancelled = ctx.product_store.cancel_turn(
                turn_id=turn_id,
                user_id=user_id,
                now=datetime.now(timezone.utc),
                error_code="cancelled_by_user",
            )
        except AgentResourceNotFound as exc:
            raise _not_found() from exc
        except Exception as exc:
            raise _conflict("Agent turn is no longer running") from exc
        return _turn_response(cancelled)


    @router.get(
        "/threads/{thread_id}/stream",
        response_model=AgentStreamEvent,
        response_class=StreamingResponse,
    )
    async def stream_thread(
        thread_id: str,
        request: Request,
        once: bool = False,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
        user_id: str = Depends(session_dependency),
        ctx: AgentApiContext = Depends(current_context),
    ) -> StreamingResponse:
        _owned_thread(ctx, thread_id, user_id)

        async def generate():
            seen: set[str] = set()
            cursor_found = last_event_id is None
            while True:
                turns = ctx.product_store.list_turns(thread_id=thread_id, user_id=user_id, limit=100)
                messages = ctx.product_store.list_messages(thread_id=thread_id, user_id=user_id, limit=100)
                try:
                    pending_interrupt = _pending_graph_interrupt(ctx, thread_id, user_id, turns=turns)
                except HTTPException:
                    pending_interrupt = None
                events = project_stream_events(turns, messages, pending_interrupt)
                if not cursor_found and not any(event.event_id == last_event_id for event in events):
                    # 游标已超出窗口时重放当前快照；客户端随后用 REST 快照去重。
                    cursor_found = True
                emitted = False
                for event in events:
                    if not cursor_found:
                        if event.event_id == last_event_id:
                            cursor_found = True
                        continue
                    if event.event_id in seen:
                        continue
                    seen.add(event.event_id)
                    emitted = True
                    yield _sse(event)
                if once:
                    break
                if not emitted:
                    yield ": keep-alive\n\n"
                if await request.is_disconnected():
                    break
                await asyncio.sleep(ctx.stream_poll_seconds)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router


def project_stream_events(
    turns: list[AgentTurnRecord],
    messages: list[AgentMessageRecord],
    pending_interrupt: GraphPendingInterrupt | None = None,
) -> list[AgentStreamEvent]:
    projected: list[tuple[datetime, AgentStreamEvent]] = []
    for turn in turns:
        event = AgentStreamEvent(
            event_id=f"turn:{turn.turn_id}:{turn.updated_at.isoformat()}",
            event_type="turn.updated",
            data=_turn_response(turn),
        )
        projected.append((turn.updated_at, event))
    for message in messages:
        event = AgentStreamEvent(
            event_id=f"message:{message.message_id}",
            event_type="message.created",
            data=_message_response(message),
        )
        projected.append((message.created_at, event))
    if pending_interrupt is not None:
        fingerprint = sha256(pending_interrupt.model_dump_json().encode("utf-8")).hexdigest()
        event_id = f"interrupt:{pending_interrupt.checkpoint_turn_id}:{fingerprint}"
        projected.append((
            datetime.now(timezone.utc),
            AgentStreamEvent(
                event_id=event_id,
                event_type="interrupt.updated",
                data=pending_interrupt,
            ),
        ))
    projected.sort(key=lambda item: (item[0], item[1].event_id))
    return [item[1] for item in projected]


def _owned_thread(ctx: AgentApiContext, thread_id: str, user_id: str) -> AgentThreadRecord:
    try:
        return ctx.product_store.get_thread(thread_id=thread_id, user_id=user_id)
    except AgentResourceNotFound as exc:
        raise _not_found() from exc


def _require_owned_jobs(ctx: AgentApiContext, job_ids: list[str], user_id: str) -> None:
    for job_id in job_ids:
        try:
            ctx.job_store.get_for_user(job_id, user_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                raise _not_found() from exc
            raise
        except (KeyError, LookupError) as exc:
            raise _not_found() from exc


def _owned_active_bundle(
    ctx: AgentApiContext,
    bundle_id: str,
    thread_id: str,
    user_id: str,
) -> tuple[AgentInputBundleRecord, list[AgentInputFileRecord]]:
    try:
        bundle = ctx.product_store.get_input_bundle(bundle_id=bundle_id, user_id=user_id)
        files = ctx.product_store.list_input_files(bundle_id=bundle_id, user_id=user_id)
    except AgentResourceNotFound as exc:
        raise _not_found() from exc
    if bundle.thread_id != thread_id:
        raise _not_found()
    if bundle.status.value != "active" or datetime.now(timezone.utc) >= bundle.expires_at:
        raise _conflict("Input bundle is no longer active")
    return bundle, files


def _graph_dataset_profiles(
    ctx: AgentApiContext,
    input_files: list[AgentInputFileRecord],
    user_id: str,
) -> list[DatasetProfileRef]:
    if not input_files:
        return []
    if ctx.dataset_loader is None:
        raise HTTPException(status_code=503, detail="Agent dataset loading is not configured")
    expected = {item.file_id: item for item in input_files}
    try:
        refs = ctx.dataset_loader(DatasetLoadRequest(
            user_id=user_id,
            dataset_ids=list(expected),
        ))
    except AgentResourceNotFound as exc:
        raise _not_found() from exc
    except (KeyError, LookupError) as exc:
        raise _not_found() from exc
    if len({item.dataset_id for item in refs}) != len(refs):
        raise _conflict("Dataset loader returned duplicate inputs")
    loaded = {item.dataset_id: item for item in refs}
    if set(loaded) != set(expected):
        raise _conflict("Dataset inputs changed before graph invocation")
    for dataset_id, record in expected.items():
        ref = loaded[dataset_id]
        if (
            ref.owner_id != user_id
            or ref.role != record.field
            or ref.checksum.casefold() != record.checksum.casefold()
        ):
            raise _not_found()
    profiles = {
        item.role: item
        for item in build_dataset_profiles({
            ref.role: (ref.filename, ref.content) for ref in refs
        })
    }
    return [
        DatasetProfileRef(
            dataset_id=ref.dataset_id,
            owner_id=ref.owner_id,
            filename=ref.filename,
            checksum=ref.checksum,
            profile=profiles[ref.role],
        )
        for ref in refs
    ]


def _graph_config(thread_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": thread_id}}


def _empty_focus() -> RunFocus:
    return RunFocus(
        in_scope_job_ids=[],
        resolved_entities={},
        last_citation=None,
    )


def _initialize_graph_checkpoint(
    ctx: AgentApiContext,
    thread_id: str,
    user_id: str,
    focus: RunFocus,
) -> None:
    state = GraphState(
        thread_id=thread_id,
        user_id=user_id,
        user_message="Thread initialized",
        focus=focus,
        version=0,
    )
    ctx.graph.update_state(
        _graph_config(thread_id),
        state.model_dump(mode="json"),
    )


def _owned_graph_focus(
    ctx: AgentApiContext,
    thread_id: str,
    user_id: str,
) -> tuple[RunFocus, int]:
    try:
        snapshot = ctx.graph.get_state(_graph_config(thread_id))
    except Exception as exc:
        raise _conflict("Agent graph checkpoint is unavailable") from exc
    values = getattr(snapshot, "values", {}) or {}
    if isinstance(values, GraphState):
        values = values.model_dump(mode="python")
    if not values:
        return _empty_focus(), 0
    if values.get("thread_id") != thread_id or values.get("user_id") != user_id:
        raise _not_found()
    try:
        focus = RunFocus.model_validate(values.get("focus", _empty_focus().model_dump()))
        version = int(values.get("version", 0))
    except (TypeError, ValueError) as exc:
        raise _conflict("Agent graph checkpoint is invalid") from exc
    return focus, version


def _owned_graph_snapshot(
    ctx: AgentApiContext,
    config: dict[str, dict[str, str]],
    thread_id: str,
    user_id: str,
):
    try:
        snapshot = ctx.graph.get_state(config)
        state = GraphState.model_validate(snapshot.values)
    except Exception as exc:
        raise _conflict("Agent graph checkpoint is unavailable") from exc
    if state.thread_id != thread_id or state.user_id != user_id:
        raise _not_found()
    return snapshot, state


def _snapshot_interrupts(snapshot: object) -> list[GraphInterrupt]:
    return [
        GraphInterrupt(interrupt_id=item.id, payload=item.value)
        for task in getattr(snapshot, "tasks", ())
        for item in task.interrupts
    ]


def _pending_graph_interrupt(
    ctx: AgentApiContext,
    thread_id: str,
    user_id: str,
    *,
    turns: list[AgentTurnRecord] | None = None,
) -> GraphPendingInterrupt | None:
    snapshot, _ = _owned_graph_snapshot(ctx, _graph_config(thread_id), thread_id, user_id)
    interrupts = _snapshot_interrupts(snapshot)
    if not interrupts:
        return None
    if len(interrupts) != 1:
        raise _conflict("Agent graph has multiple pending interrupts")
    records = turns if turns is not None else ctx.product_store.list_turns(
        thread_id=thread_id,
        user_id=user_id,
        limit=100,
    )
    # A queued resume still has the previous checkpoint interrupt until the
    # runtime consumes it; hide that stale input to prevent duplicate submits.
    active = [turn for turn in records if turn.status is AgentTurnStatus.RUNNING]
    if not active:
        return None
    turn = max(active, key=lambda item: (item.updated_at, item.turn_id))
    return GraphPendingInterrupt(
        checkpoint_turn_id=turn.turn_id,
        interrupt=interrupts[0],
    )


def _queued_graph_turn_result(turn: AgentTurnRecord) -> GraphTurnResult:
    return GraphTurnResult(
        checkpoint_turn_id=turn.turn_id,
        turn=_turn_response(turn),
    )


def _enqueue_work(ctx: AgentApiContext, item: AgentTurnWorkItem) -> None:
    if ctx.turn_queue is None:
        raise HTTPException(status_code=503, detail="Agent runtime queue is not configured")
    try:
        ctx.turn_queue.enqueue(item)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Agent turn could not be queued") from exc


def _delete_graph_checkpoint(ctx: AgentApiContext, thread_id: str) -> None:
    checkpointer = getattr(ctx.graph, "checkpointer", None)
    delete_thread = getattr(checkpointer, "delete_thread", None)
    if callable(delete_thread):
        delete_thread(thread_id)


def _thread_response(record: AgentThreadRecord) -> AgentThreadResponse:
    return AgentThreadResponse.model_validate(record.model_dump(exclude={"user_id"}))


def _run_response(
    run_id: str,
    thread_id: str,
    focus: RunFocus,
    version: int,
) -> AgentRunResponse:
    return AgentRunResponse(
        run_id=run_id,
        thread_id=thread_id,
        focus=focus,
        version=version,
    )


def _turn_response(record: AgentTurnRecord) -> AgentTurnResponse:
    return AgentTurnResponse.model_validate(record.model_dump(exclude={
        "user_id", "idempotency_key", "request_hash",
    }))


def _message_response(record: AgentMessageRecord) -> AgentMessageResponse:
    return AgentMessageResponse.model_validate(record.model_dump(exclude={"user_id"}))


def _file_response(record: AgentInputFileRecord) -> AgentInputFileResponse:
    return AgentInputFileResponse(
        file_id=record.file_id,
        field=record.field,
        filename=record.filename,
        checksum=record.checksum,
        content_type=record.content_type,
        size_bytes=record.size_bytes,
        created_at=record.created_at,
    )


def _bundle_response(
    bundle: AgentInputBundleRecord,
    files: list[AgentInputFileRecord],
) -> AgentInputBundleResponse:
    return AgentInputBundleResponse(
        bundle_id=bundle.bundle_id,
        thread_id=bundle.thread_id,
        status=bundle.status,
        expires_at=bundle.expires_at,
        created_at=bundle.created_at,
        files=[_file_response(item) for item in files],
    )


def _request_hash(thread_id: str, payload: dict) -> str:
    canonical = json.dumps(
        {"thread_id": thread_id, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "sha256:" + sha256(canonical.encode("utf-8")).hexdigest()


def _after_id(records: list, after: str | None, field: str) -> list:
    if after is None:
        return records
    for index, record in enumerate(records):
        if getattr(record, field) == after:
            return records[index + 1:]
    raise HTTPException(status_code=400, detail="Invalid cursor")


def _sse(event: AgentStreamEvent) -> str:
    return (
        f"id: {event.event_id}\n"
        f"event: {event.event_type}\n"
        f"data: {event.model_dump_json()}\n\n"
    )


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Agent resource not found")


def _conflict(message: str) -> HTTPException:
    return HTTPException(status_code=409, detail=message)
