from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Annotated, Callable
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse

from ..storage_service import AGENT_BUNDLE_MAX_BYTES
from .approvals import ApprovalExpired, ApprovalMismatch, ApprovalNotFound
from .bootstrap import AgentApiContext
from .plans import PlanNotFound
from .product_store import ActiveTurnConflict, AgentResourceNotFound, IdempotencyConflict
from .schemas import (
    ActiveProfile,
    AgentApprovalBlock,
    AgentApprovalDecision,
    AgentApprovalRequest,
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
    AgentState,
    AgentStreamEvent,
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
    ApprovalStatus,
    RunFocus,
    RunState,
    RunStatus,
)
from .store import StateConflict, StateNotFound


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
        state = RunState(
            run_id=run_id,
            user_id=user_id,
            thread_id=thread_id,
            active_profile=ActiveProfile.ANALYSIS,
            state=AgentState.COLLECT_INTENT,
            step_no=0,
            plan_id=None,
            plan_hash=None,
            pending_approval_id=None,
            focus=RunFocus(
                in_scope_job_ids=list(payload.focus_job_ids),
                resolved_entities={},
                last_citation=None,
            ),
            model_calls=0,
            tool_calls=0,
            status=RunStatus.RUNNING,
            version=0,
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
            ctx.state_store.save(state, expected_version=0)
            ctx.product_store.save_thread(thread)
        except StateConflict as exc:
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
        state = _owned_state(ctx, thread.current_run_id, user_id)
        return AgentThreadDetailResponse(thread=_thread_response(thread), run=_run_response(state))

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

    @router.post("/threads/{thread_id}/turns", response_model=AgentTurnResponse, status_code=202)
    def create_turn(
        thread_id: str,
        payload: AgentTurnCreateRequest,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)],
        user_id: str = Depends(session_dependency),
        ctx: AgentApiContext = Depends(current_context),
    ) -> AgentTurnResponse:
        thread = _owned_thread(ctx, thread_id, user_id)
        state = _owned_state(ctx, thread.current_run_id, user_id)
        _require_owned_jobs(ctx, payload.focus_job_ids, user_id)
        blocks = [AgentTextBlock(text=payload.message)]
        if payload.input_bundle_id:
            bundle, input_files = _owned_active_bundle(ctx, payload.input_bundle_id, thread_id, user_id)
            blocks.append(AgentInputSummaryBlock(
                bundle_id=bundle.bundle_id,
                files=[_file_response(item) for item in input_files],
            ))
        now = datetime.now(timezone.utc)
        turn_id = f"turn-{uuid4()}"
        turn = AgentTurnRecord(
            turn_id=turn_id,
            thread_id=thread_id,
            run_id=thread.current_run_id,
            user_id=user_id,
            idempotency_key=idempotency_key,
            request_hash=_request_hash(thread_id, payload.model_dump(mode="json")),
            status=AgentTurnStatus.QUEUED,
            attempt=0,
            lease_owner=None,
            lease_expires_at=None,
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
            user_id=user_id,
            role=AgentMessageRole.USER,
            blocks=blocks,
            created_at=now,
        )
        desired_focus = None
        if payload.focus_job_ids:
            desired_focus = state.focus.model_copy(update={"in_scope_job_ids": list(payload.focus_job_ids)})
        try:
            queued, created = ctx.product_store.enqueue_turn(
                message=message,
                turn=turn,
                focus=desired_focus,
                expected_run_version=state.version if desired_focus is not None else None,
            )
            if created and desired_focus is not None:
                _persist_non_transactional_focus_if_needed(ctx, state.run_id, user_id, desired_focus)
        except (IdempotencyConflict, ActiveTurnConflict, StateConflict) as exc:
            raise _conflict(str(exc) or "Agent turn conflicts with current state") from exc
        return _turn_response(queued)

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

    @router.post(
        "/threads/{thread_id}/approvals/{approval_id}",
        response_model=AgentTurnResponse | AgentMessageResponse,
    )
    def decide_approval(
        thread_id: str,
        approval_id: str,
        payload: AgentApprovalRequest,
        response: Response,
        user_id: str = Depends(session_dependency),
        ctx: AgentApiContext = Depends(current_context),
    ) -> AgentTurnResponse | AgentMessageResponse:
        thread = _owned_thread(ctx, thread_id, user_id)
        state = _owned_state(ctx, thread.current_run_id, user_id)
        try:
            approval = ctx.approval_gate.get_owned(approval_id=approval_id, user_id=user_id)
            if not state.plan_id:
                raise ApprovalMismatch("run has no pending plan")
            plan = ctx.plan_store.get(plan_id=state.plan_id, user_id=user_id)
        except ApprovalMismatch as exc:
            # 计划已被自动作废（例如待批期改参数）：旧卡片不可再用，返回可读冲突而非 500。
            raise _conflict(str(exc)) from exc
        except (ApprovalNotFound, PlanNotFound) as exc:
            raise _not_found() from exc
        if (
            state.pending_approval_id != approval_id
            or plan.approval_id != approval_id
            or plan.thread_id != thread_id
            or plan.run_id != state.run_id
            or approval.run_id != state.run_id
            or payload.plan_hash != state.plan_hash
            or payload.plan_hash != plan.plan_hash
            or payload.plan_hash != approval.plan_hash
        ):
            raise _conflict("Approval does not match the current plan")
        now = datetime.now(timezone.utc)
        if payload.decision is AgentApprovalDecision.REJECT:
            try:
                ctx.approval_gate.reject(
                    approval_id=approval_id,
                    run_id=state.run_id,
                    user_id=user_id,
                    plan_hash=payload.plan_hash,
                )
            except ApprovalExpired as exc:
                _release_expired_approval(ctx, state, approval_id, user_id)
                raise _conflict("Approval has expired; generate a new plan to continue") from exc
            except ApprovalMismatch as exc:
                raise _conflict(str(exc)) from exc
            state.pending_approval_id = None
            state.state = AgentState.NEED_USER_INPUT
            state.status = RunStatus.RUNNING
            try:
                ctx.state_store.save(state, expected_version=state.version)
            except StateConflict as exc:
                raise _conflict(str(exc)) from exc
            message = AgentMessageRecord(
                message_id=f"approval-message-{approval_id}-rejected",
                thread_id=thread_id,
                run_id=state.run_id,
                user_id=user_id,
                role=AgentMessageRole.ASSISTANT,
                blocks=[AgentApprovalBlock(
                    approval_id=approval_id,
                    plan_hash=payload.plan_hash,
                    status=ApprovalStatus.REJECTED,
                    expires_at=approval.expires_at,
                )],
                created_at=now,
            )
            ctx.product_store.append_message(message)
            return _message_response(message)

        try:
            ctx.approval_gate.resume(
                approval_id=approval_id,
                run_id=state.run_id,
                user_id=user_id,
                plan_hash=payload.plan_hash,
            )
            turn = AgentTurnRecord(
                turn_id=f"turn-{uuid4()}",
                thread_id=thread_id,
                run_id=state.run_id,
                user_id=user_id,
                idempotency_key=f"approval:{approval_id}:approve",
                request_hash=_request_hash(thread_id, payload.model_dump(mode="json")),
                status=AgentTurnStatus.QUEUED,
                attempt=0,
                lease_owner=None,
                lease_expires_at=None,
                error_code=None,
                created_at=now,
                updated_at=now,
                started_at=None,
                completed_at=None,
            )
            approval_message = AgentMessageRecord(
                message_id=f"approval-message-{approval_id}-approved",
                thread_id=thread_id,
                run_id=state.run_id,
                user_id=user_id,
                role=AgentMessageRole.ASSISTANT,
                blocks=[AgentApprovalBlock(
                    approval_id=approval_id,
                    plan_hash=payload.plan_hash,
                    status=ApprovalStatus.APPROVED,
                    expires_at=approval.expires_at,
                )],
                created_at=now,
            )
            queued, _ = ctx.product_store.enqueue_turn(message=approval_message, turn=turn)
        except ApprovalExpired as exc:
            _release_expired_approval(ctx, state, approval_id, user_id)
            raise _conflict("Approval has expired; generate a new plan to continue") from exc
        except (ApprovalMismatch, IdempotencyConflict, ActiveTurnConflict) as exc:
            raise _conflict(str(exc)) from exc
        response.status_code = 202
        return _turn_response(queued)

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
                events = project_stream_events(turns, messages)
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
    projected.sort(key=lambda item: (item[0], item[1].event_id))
    return [item[1] for item in projected]


def _owned_thread(ctx: AgentApiContext, thread_id: str, user_id: str) -> AgentThreadRecord:
    try:
        return ctx.product_store.get_thread(thread_id=thread_id, user_id=user_id)
    except AgentResourceNotFound as exc:
        raise _not_found() from exc


def _owned_state(ctx: AgentApiContext, run_id: str, user_id: str) -> RunState:
    try:
        return ctx.state_store.get(run_id=run_id, user_id=user_id)
    except StateNotFound as exc:
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


def _persist_non_transactional_focus_if_needed(
    ctx: AgentApiContext,
    run_id: str,
    user_id: str,
    focus: RunFocus,
) -> None:
    current = ctx.state_store.get(run_id=run_id, user_id=user_id)
    if current.focus == focus:
        return
    current.focus = focus
    ctx.state_store.save(current, expected_version=current.version)


def _release_expired_approval(
    ctx: AgentApiContext,
    state: RunState,
    approval_id: str,
    user_id: str,
) -> None:
    if state.pending_approval_id != approval_id:
        return
    state.pending_approval_id = None
    state.state = AgentState.NEED_USER_INPUT
    state.status = RunStatus.RUNNING
    try:
        ctx.state_store.save(state, expected_version=state.version)
    except StateConflict as exc:
        raise _conflict(str(exc)) from exc


def _thread_response(record: AgentThreadRecord) -> AgentThreadResponse:
    return AgentThreadResponse.model_validate(record.model_dump(exclude={"user_id"}))


def _run_response(state: RunState) -> AgentRunResponse:
    return AgentRunResponse.model_validate(state.model_dump(exclude={"user_id"}))


def _turn_response(record: AgentTurnRecord) -> AgentTurnResponse:
    return AgentTurnResponse.model_validate(record.model_dump(exclude={
        "user_id", "idempotency_key", "request_hash", "lease_owner", "lease_expires_at",
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
