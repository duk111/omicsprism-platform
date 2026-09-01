from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from typing import Any, Protocol

from .schemas import (
    AgentEvalCandidateRecord,
    AgentFeedbackRecord,
    AgentInputBundleRecord,
    AgentInputFileRecord,
    AgentMessageRecord,
    AgentMessageRole,
    AgentThreadRecord,
    AgentTurnRecord,
    AgentTurnStatus,
)
from .job_events import (
    AgentJobCompletionEvent,
    AgentJobWaitRecord,
    AgentJobWaitStatus,
    continuation_idempotency_key,
    continuation_turn_id,
)
from .trace import AgentTraceEvent


class AgentResourceNotFound(LookupError):
    pass


class IdempotencyConflict(RuntimeError):
    pass


class ActiveTurnConflict(RuntimeError):
    pass


class TurnConflict(RuntimeError):
    pass


class AgentProductStore(Protocol):
    def record_trace_event(self, event: AgentTraceEvent) -> None:
        ...

    def list_trace_events(
        self, *, trace_id: str, user_id: str, limit: int = 100,
    ) -> list[AgentTraceEvent]:
        ...

    def get_feedback_target(
        self, *, thread_id: str, message_id: str, user_id: str,
    ) -> tuple[AgentMessageRecord, AgentTurnRecord, AgentMessageRecord | None]:
        ...

    def save_feedback(self, feedback: AgentFeedbackRecord) -> AgentFeedbackRecord:
        ...

    def list_feedback(
        self, *, thread_id: str, user_id: str, limit: int = 100,
    ) -> list[AgentFeedbackRecord]:
        ...

    def save_eval_candidate(
        self, candidate: AgentEvalCandidateRecord,
    ) -> AgentEvalCandidateRecord:
        ...

    def get_eval_candidate(
        self, *, candidate_id: str, user_id: str,
    ) -> AgentEvalCandidateRecord:
        ...

    def list_eval_candidates_for_review(
        self, *, limit: int = 100,
    ) -> list[AgentEvalCandidateRecord]:
        ...

    def delete_eval_candidate_for_feedback(
        self, *, feedback_id: str, user_id: str,
    ) -> None:
        ...

    def save_thread(self, thread: AgentThreadRecord) -> None:
        ...

    def get_thread(self, *, thread_id: str, user_id: str) -> AgentThreadRecord:
        ...

    def list_threads(self, *, user_id: str, limit: int = 100) -> list[AgentThreadRecord]:
        ...

    def delete_thread(self, *, thread_id: str, user_id: str) -> list[AgentInputFileRecord]:
        ...

    def append_message(self, message: AgentMessageRecord) -> None:
        ...

    def list_messages(self, *, thread_id: str, user_id: str, limit: int = 100) -> list[AgentMessageRecord]:
        ...

    def create_turn(self, turn: AgentTurnRecord) -> AgentTurnRecord:
        ...

    def enqueue_turn(
        self,
        *,
        message: AgentMessageRecord | None,
        turn: AgentTurnRecord,
    ) -> tuple[AgentTurnRecord, bool]:
        ...

    def get_turn(self, *, turn_id: str, user_id: str) -> AgentTurnRecord:
        ...

    def claim_turn(self, *, turn_id: str, user_id: str, now: datetime) -> AgentTurnRecord:
        ...

    def queue_turn(self, *, turn_id: str, user_id: str, now: datetime) -> AgentTurnRecord:
        ...

    def list_turns(self, *, thread_id: str, user_id: str, limit: int = 100) -> list[AgentTurnRecord]:
        ...

    def finish_turn(
        self, *, turn_id: str, user_id: str, status: AgentTurnStatus, now: datetime,
        message: AgentMessageRecord | None = None, error_code: str | None = None,
    ) -> AgentTurnRecord:
        ...

    def cancel_turn(self, *, turn_id: str, user_id: str, now: datetime, error_code: str) -> AgentTurnRecord:
        ...

    def save_input_bundle(self, bundle: AgentInputBundleRecord) -> None:
        ...

    def save_input_bundle_with_files(self, *, bundle: AgentInputBundleRecord,
                                     files: list[AgentInputFileRecord]) -> None:
        ...

    def get_input_bundle(self, *, bundle_id: str, user_id: str) -> AgentInputBundleRecord:
        ...

    def append_input_file(self, item: AgentInputFileRecord) -> None:
        ...

    def list_input_files(self, *, bundle_id: str, user_id: str) -> list[AgentInputFileRecord]:
        ...

    def get_input_file(self, *, file_id: str, user_id: str) -> AgentInputFileRecord:
        ...

    def get_latest_active_bundle(self, *, thread_id: str, user_id: str, before: datetime) -> AgentInputBundleRecord | None:
        ...

    def create_job_wait(self, wait: AgentJobWaitRecord) -> AgentJobWaitRecord:
        ...

    def get_job_wait(self, *, job_id: str, user_id: str) -> AgentJobWaitRecord:
        ...

    def list_job_waits(
        self, *, thread_id: str, user_id: str, limit: int = 100,
    ) -> list[AgentJobWaitRecord]:
        ...

    def cancel_job_wait(
        self, *, job_id: str, user_id: str, now: datetime,
    ) -> AgentJobWaitRecord:
        ...

    def save_job_completion_event(self, event: AgentJobCompletionEvent) -> None:
        ...

    def list_pending_job_events(self, *, limit: int = 20) -> list[AgentJobCompletionEvent]:
        ...

    def prepare_job_continuation(
        self, event: AgentJobCompletionEvent, *, now: datetime,
    ) -> AgentTurnRecord | None:
        ...

    def mark_job_event_published(self, *, event_id: str, now: datetime) -> None:
        ...

    def mark_job_event_failed(self, *, event_id: str, error: str, now: datetime) -> None:
        ...

    def complete_job_wait(
        self, *, job_id: str, user_id: str, continuation_turn_id: str,
        status: AgentJobWaitStatus, now: datetime,
    ) -> AgentJobWaitRecord:
        ...


class InMemoryAgentProductStore:
    """普通 CI 使用的精确 repository 契约，不生成业务假数据。"""

    def __init__(self, shared: dict[str, Any] | None = None) -> None:
        self._shared = shared if shared is not None else {}
        self._threads = self._shared.setdefault("threads", {})
        self._messages = self._shared.setdefault("messages", {})
        self._turns = self._shared.setdefault("turns", {})
        self._turn_keys = self._shared.setdefault("turn_keys", {})
        self._bundles = self._shared.setdefault("bundles", {})
        self._files = self._shared.setdefault("files", {})
        self._trace_events = self._shared.setdefault("trace_events", {})
        self._feedback = self._shared.setdefault("feedback", {})
        self._eval_candidates = self._shared.setdefault("eval_candidates", {})
        self._candidate_feedback_ids = self._shared.setdefault("candidate_feedback_ids", {})
        self._job_waits = self._shared.setdefault("job_waits", {})
        self._job_events = self._shared.setdefault("job_events", {})

    def record_trace_event(self, event: AgentTraceEvent) -> None:
        if event.event_id in self._trace_events:
            return
        self.get_thread(thread_id=event.thread_id, user_id=event.user_id)
        self._trace_events[event.event_id] = event.model_dump(mode="json")

    def list_trace_events(
        self, *, trace_id: str, user_id: str, limit: int = 100,
    ) -> list[AgentTraceEvent]:
        bounded = max(1, min(limit, 500))
        records = [
            AgentTraceEvent.model_validate(deepcopy(payload))
            for payload in self._trace_events.values()
            if payload["trace_id"] == trace_id and payload["user_id"] == user_id
        ]
        records.sort(key=lambda item: (item.created_at, item.event_id))
        return records[-bounded:]

    def get_feedback_target(
        self, *, thread_id: str, message_id: str, user_id: str,
    ) -> tuple[AgentMessageRecord, AgentTurnRecord, AgentMessageRecord | None]:
        self.get_thread(thread_id=thread_id, user_id=user_id)
        payload = self._messages.get(message_id)
        if (
            payload is None
            or payload["thread_id"] != thread_id
            or payload["user_id"] != user_id
            or payload["role"] != AgentMessageRole.ASSISTANT.value
            or not message_id.startswith("assistant-")
        ):
            raise AgentResourceNotFound(message_id)
        assistant = AgentMessageRecord.model_validate(deepcopy(payload))
        turn_id = message_id.removeprefix("assistant-")
        turn = self.get_turn(turn_id=turn_id, user_id=user_id)
        if (
            turn.thread_id != thread_id
            or turn.run_id != assistant.run_id
            or turn.trace_id != assistant.trace_id
        ):
            raise AgentResourceNotFound(message_id)
        user_payload = self._messages.get(f"user-{turn_id}")
        user_message = (
            AgentMessageRecord.model_validate(deepcopy(user_payload))
            if user_payload is not None
            and user_payload["thread_id"] == thread_id
            and user_payload["user_id"] == user_id
            and user_payload["role"] == AgentMessageRole.USER.value
            and user_payload["run_id"] == turn.run_id
            and user_payload["trace_id"] == turn.trace_id
            else None
        )
        return assistant, turn, user_message

    def save_feedback(self, feedback: AgentFeedbackRecord) -> AgentFeedbackRecord:
        _assistant, turn, _ = self.get_feedback_target(
            thread_id=feedback.thread_id,
            message_id=feedback.message_id,
            user_id=feedback.user_id,
        )
        if (turn.turn_id, turn.trace_id) != (feedback.turn_id, feedback.trace_id):
            raise AgentResourceNotFound(feedback.message_id)
        key = (feedback.message_id, feedback.user_id)
        current_id = self._feedback.get(key)
        if current_id is not None:
            current = AgentFeedbackRecord.model_validate(deepcopy(self._feedback[current_id]))
            feedback = feedback.model_copy(update={
                "feedback_id": current.feedback_id,
                "created_at": current.created_at,
            })
        self._feedback[key] = feedback.feedback_id
        self._feedback[feedback.feedback_id] = feedback.model_dump(mode="json")
        return feedback.model_copy(deep=True)

    def list_feedback(
        self, *, thread_id: str, user_id: str, limit: int = 100,
    ) -> list[AgentFeedbackRecord]:
        self.get_thread(thread_id=thread_id, user_id=user_id)
        bounded = max(1, min(limit, 100))
        records = [
            AgentFeedbackRecord.model_validate(deepcopy(payload))
            for key, payload in self._feedback.items()
            if isinstance(key, str)
            and payload["thread_id"] == thread_id
            and payload["user_id"] == user_id
        ]
        records.sort(key=lambda item: (item.updated_at, item.feedback_id), reverse=True)
        return records[:bounded]

    def save_eval_candidate(
        self, candidate: AgentEvalCandidateRecord,
    ) -> AgentEvalCandidateRecord:
        if candidate.status.value != "pending_review":
            raise ValueError("candidate review status is controlled outside the public feedback flow")
        feedback_payload = self._feedback.get(candidate.feedback_id)
        if feedback_payload is None or not isinstance(feedback_payload, dict):
            raise AgentResourceNotFound(candidate.feedback_id)
        feedback = AgentFeedbackRecord.model_validate(deepcopy(feedback_payload))
        if (
            feedback.user_id, feedback.thread_id, feedback.turn_id, feedback.message_id,
            feedback.trace_id, feedback.rating, feedback.failure_category
        ) != (
            candidate.user_id, candidate.thread_id, candidate.turn_id, candidate.message_id,
            candidate.trace_id, candidate.rating, candidate.failure_category
        ):
            raise AgentResourceNotFound(candidate.feedback_id)
        current_id = self._candidate_feedback_ids.get(candidate.feedback_id)
        if current_id is not None:
            current = AgentEvalCandidateRecord.model_validate(
                deepcopy(self._eval_candidates[current_id])
            )
            candidate = candidate.model_copy(update={
                "candidate_id": current.candidate_id,
                "created_at": current.created_at,
            })
        self._candidate_feedback_ids[candidate.feedback_id] = candidate.candidate_id
        self._eval_candidates[candidate.candidate_id] = candidate.model_dump(mode="json")
        return candidate.model_copy(deep=True)

    def get_eval_candidate(
        self, *, candidate_id: str, user_id: str,
    ) -> AgentEvalCandidateRecord:
        payload = self._eval_candidates.get(candidate_id)
        if payload is None or payload["user_id"] != user_id:
            raise AgentResourceNotFound(candidate_id)
        return AgentEvalCandidateRecord.model_validate(deepcopy(payload))

    def list_eval_candidates_for_review(
        self, *, limit: int = 100,
    ) -> list[AgentEvalCandidateRecord]:
        bounded = max(1, min(limit, 1000))
        records = [
            AgentEvalCandidateRecord.model_validate(deepcopy(payload))
            for payload in self._eval_candidates.values()
            if payload["status"] == "pending_review"
        ]
        records.sort(key=lambda item: (item.updated_at, item.candidate_id))
        return records[:bounded]

    def delete_eval_candidate_for_feedback(
        self, *, feedback_id: str, user_id: str,
    ) -> None:
        candidate_id = self._candidate_feedback_ids.get(feedback_id)
        if candidate_id is None:
            return
        payload = self._eval_candidates.get(candidate_id)
        if payload is None or payload["user_id"] != user_id:
            raise AgentResourceNotFound(feedback_id)
        del self._candidate_feedback_ids[feedback_id]
        del self._eval_candidates[candidate_id]

    def save_thread(self, thread: AgentThreadRecord) -> None:
        current = self._threads.get(thread.thread_id)
        if current is not None and current["user_id"] != thread.user_id:
            raise AgentResourceNotFound(thread.thread_id)
        self._threads[thread.thread_id] = thread.model_dump(mode="json")

    def get_thread(self, *, thread_id: str, user_id: str) -> AgentThreadRecord:
        payload = self._threads.get(thread_id)
        if payload is None or payload["user_id"] != user_id:
            raise AgentResourceNotFound(thread_id)
        return AgentThreadRecord.model_validate(deepcopy(payload))

    def list_threads(self, *, user_id: str, limit: int = 100) -> list[AgentThreadRecord]:
        bounded = max(1, min(limit, 100))
        records = [
            AgentThreadRecord.model_validate(deepcopy(payload))
            for payload in self._threads.values()
            if payload["user_id"] == user_id
        ]
        records.sort(key=lambda item: (item.updated_at, item.thread_id), reverse=True)
        return records[:bounded]

    def delete_thread(self, *, thread_id: str, user_id: str) -> list[AgentInputFileRecord]:
        self.get_thread(thread_id=thread_id, user_id=user_id)
        files = [
            AgentInputFileRecord.model_validate(deepcopy(payload))
            for payload in self._files.values()
            if payload["user_id"] == user_id
            and any(
                bundle["bundle_id"] == payload["bundle_id"]
                and bundle["thread_id"] == thread_id
                and bundle["user_id"] == user_id
                for bundle in self._bundles.values()
            )
        ]
        bundle_ids = {
            bundle_id for bundle_id, payload in self._bundles.items()
            if payload["thread_id"] == thread_id and payload["user_id"] == user_id
        }
        for file_id, payload in list(self._files.items()):
            if payload["bundle_id"] in bundle_ids and payload["user_id"] == user_id:
                del self._files[file_id]
        for bundle_id in bundle_ids:
            del self._bundles[bundle_id]
        for message_id, payload in list(self._messages.items()):
            if payload["thread_id"] == thread_id and payload["user_id"] == user_id:
                del self._messages[message_id]
        for turn_id, payload in list(self._turns.items()):
            if payload["thread_id"] == thread_id and payload["user_id"] == user_id:
                self._turn_keys.pop((user_id, payload["idempotency_key"]), None)
                del self._turns[turn_id]
        for event_id, payload in list(self._trace_events.items()):
            if payload["thread_id"] == thread_id and payload["user_id"] == user_id:
                del self._trace_events[event_id]
        for feedback_id, payload in list(self._feedback.items()):
            if isinstance(feedback_id, str) and isinstance(payload, dict) and (
                payload["thread_id"] == thread_id and payload["user_id"] == user_id
            ):
                self._feedback.pop((payload["message_id"], payload["user_id"]), None)
                del self._feedback[feedback_id]
        for candidate_id, payload in list(self._eval_candidates.items()):
            if payload["thread_id"] == thread_id and payload["user_id"] == user_id:
                self._candidate_feedback_ids.pop(payload["feedback_id"], None)
                del self._eval_candidates[candidate_id]
        for wait_id, payload in list(self._job_waits.items()):
            if payload["thread_id"] == thread_id and payload["user_id"] == user_id:
                del self._job_waits[wait_id]
        for event_id, payload in list(self._job_events.items()):
            if payload["thread_id"] == thread_id and payload["user_id"] == user_id:
                del self._job_events[event_id]
        del self._threads[thread_id]
        return files

    def append_message(self, message: AgentMessageRecord) -> None:
        self.get_thread(thread_id=message.thread_id, user_id=message.user_id)
        if message.message_id in self._messages:
            raise ValueError("agent message id already exists")
        self._messages[message.message_id] = message.model_dump(mode="json")

    def list_messages(self, *, thread_id: str, user_id: str, limit: int = 100) -> list[AgentMessageRecord]:
        self.get_thread(thread_id=thread_id, user_id=user_id)
        bounded = max(1, min(limit, 100))
        records = [
            AgentMessageRecord.model_validate(deepcopy(payload))
            for payload in self._messages.values()
            if payload["thread_id"] == thread_id and payload["user_id"] == user_id
        ]
        records.sort(key=lambda item: (item.created_at, item.message_id))
        return records[-bounded:]

    def create_turn(self, turn: AgentTurnRecord) -> AgentTurnRecord:
        return self.enqueue_turn(message=None, turn=turn)[0]

    def enqueue_turn(
        self,
        *,
        message: AgentMessageRecord | None,
        turn: AgentTurnRecord,
    ) -> tuple[AgentTurnRecord, bool]:
        self.get_thread(thread_id=turn.thread_id, user_id=turn.user_id)
        if message is not None:
            if (message.thread_id, message.run_id, message.user_id) != (
                turn.thread_id, turn.run_id, turn.user_id,
            ):
                raise ValueError("turn message does not match turn ownership")
        key = (turn.user_id, turn.idempotency_key)
        existing_id = self._turn_keys.get(key)
        if existing_id is not None:
            existing = AgentTurnRecord.model_validate(deepcopy(self._turns[existing_id]))
            if (
                existing.request_hash != turn.request_hash
                or existing.thread_id != turn.thread_id
                or existing.run_id != turn.run_id
            ):
                raise IdempotencyConflict(turn.idempotency_key)
            return existing, False
        if message is not None and message.message_id in self._messages:
            raise ValueError("agent message id already exists")
        if any(
            payload["thread_id"] == turn.thread_id
            and payload["user_id"] == turn.user_id
            and payload["status"] in {AgentTurnStatus.QUEUED.value, AgentTurnStatus.RUNNING.value}
            for payload in self._turns.values()
        ):
            raise ActiveTurnConflict(turn.thread_id)
        if turn.turn_id in self._turns:
            raise ValueError("agent turn id already exists")
        self._turns[turn.turn_id] = turn.model_dump(mode="json")
        self._turn_keys[key] = turn.turn_id
        if message is not None:
            self._messages[message.message_id] = message.model_dump(mode="json")
        return turn.model_copy(deep=True), True

    def get_turn(self, *, turn_id: str, user_id: str) -> AgentTurnRecord:
        payload = self._turns.get(turn_id)
        if payload is None or payload["user_id"] != user_id:
            raise AgentResourceNotFound(turn_id)
        return AgentTurnRecord.model_validate(deepcopy(payload))

    def claim_turn(self, *, turn_id: str, user_id: str, now: datetime) -> AgentTurnRecord:
        turn = self.get_turn(turn_id=turn_id, user_id=user_id)
        if turn.status in {AgentTurnStatus.QUEUED, AgentTurnStatus.RUNNING}:
            turn.status = AgentTurnStatus.RUNNING
            turn.attempt += 1
            turn.started_at = now
            turn.updated_at = now
            turn.error_code = None
            self._turns[turn.turn_id] = turn.model_dump(mode="json")
        return turn.model_copy(deep=True)

    def queue_turn(self, *, turn_id: str, user_id: str, now: datetime) -> AgentTurnRecord:
        turn = self.get_turn(turn_id=turn_id, user_id=user_id)
        if turn.status is not AgentTurnStatus.RUNNING:
            raise TurnConflict(turn_id)
        turn.status = AgentTurnStatus.QUEUED
        turn.updated_at = now
        turn.started_at = None
        turn.completed_at = None
        turn.error_code = None
        self._turns[turn.turn_id] = turn.model_dump(mode="json")
        return turn.model_copy(deep=True)

    def list_turns(self, *, thread_id: str, user_id: str, limit: int = 100) -> list[AgentTurnRecord]:
        self.get_thread(thread_id=thread_id, user_id=user_id)
        bounded = max(1, min(limit, 100))
        records = [
            AgentTurnRecord.model_validate(deepcopy(payload))
            for payload in self._turns.values()
            if payload["thread_id"] == thread_id and payload["user_id"] == user_id
        ]
        records.sort(key=lambda item: (item.created_at, item.turn_id))
        return records[-bounded:]

    def finish_turn(
        self, *, turn_id: str, user_id: str, status: AgentTurnStatus, now: datetime,
        message: AgentMessageRecord | None = None, error_code: str | None = None,
    ) -> AgentTurnRecord:
        if status not in {AgentTurnStatus.COMPLETED, AgentTurnStatus.FAILED}:
            raise ValueError("turn may only finish as completed or failed")
        turn = self.get_turn(turn_id=turn_id, user_id=user_id)
        if turn.status is not AgentTurnStatus.RUNNING:
            raise TurnConflict(turn_id)
        if message is not None:
            if (message.thread_id, message.run_id, message.user_id) != (
                turn.thread_id, turn.run_id, turn.user_id,
            ):
                raise ValueError("turn message does not match ownership")
            self.append_message(message)
        turn.status = status
        turn.error_code = error_code
        turn.completed_at = now
        turn.updated_at = now
        self._turns[turn.turn_id] = turn.model_dump(mode="json")
        return turn.model_copy(deep=True)

    def cancel_turn(self, *, turn_id: str, user_id: str, now: datetime, error_code: str) -> AgentTurnRecord:
        turn = self.get_turn(turn_id=turn_id, user_id=user_id)
        if turn.status not in {AgentTurnStatus.QUEUED, AgentTurnStatus.RUNNING}:
            raise TurnConflict(turn_id)
        turn.status = AgentTurnStatus.CANCELLED
        turn.error_code = error_code
        turn.completed_at = now
        turn.updated_at = now
        self._turns[turn.turn_id] = turn.model_dump(mode="json")
        return turn.model_copy(deep=True)

    def save_input_bundle(self, bundle: AgentInputBundleRecord) -> None:
        self.get_thread(thread_id=bundle.thread_id, user_id=bundle.user_id)
        current = self._bundles.get(bundle.bundle_id)
        if current is not None and current["user_id"] != bundle.user_id:
            raise AgentResourceNotFound(bundle.bundle_id)
        self._bundles[bundle.bundle_id] = bundle.model_dump(mode="json")

    def save_input_bundle_with_files(self, *, bundle: AgentInputBundleRecord,
                                     files: list[AgentInputFileRecord]) -> None:
        self.get_thread(thread_id=bundle.thread_id, user_id=bundle.user_id)
        if len(files) > 6 or any(
            (item.bundle_id, item.user_id) != (bundle.bundle_id, bundle.user_id)
            for item in files
        ):
            raise ValueError("input files do not match bundle ownership")
        if len({item.field for item in files}) != len(files):
            raise ValueError("input bundle fields must be unique")
        if any(item.file_id in self._files for item in files):
            raise ValueError("agent input file id already exists")
        self._bundles[bundle.bundle_id] = bundle.model_dump(mode="json")
        for item in files:
            self._files[item.file_id] = item.model_dump(mode="json")

    def get_input_bundle(self, *, bundle_id: str, user_id: str) -> AgentInputBundleRecord:
        payload = self._bundles.get(bundle_id)
        if payload is None or payload["user_id"] != user_id:
            raise AgentResourceNotFound(bundle_id)
        return AgentInputBundleRecord.model_validate(deepcopy(payload))

    def append_input_file(self, item: AgentInputFileRecord) -> None:
        self.get_input_bundle(bundle_id=item.bundle_id, user_id=item.user_id)
        if item.file_id in self._files:
            raise ValueError("agent input file id already exists")
        self._files[item.file_id] = item.model_dump(mode="json")

    def list_input_files(self, *, bundle_id: str, user_id: str) -> list[AgentInputFileRecord]:
        self.get_input_bundle(bundle_id=bundle_id, user_id=user_id)
        records = [
            AgentInputFileRecord.model_validate(deepcopy(payload))
            for payload in self._files.values()
            if payload["bundle_id"] == bundle_id and payload["user_id"] == user_id
        ]
        records.sort(key=lambda item: (item.created_at, item.file_id))
        return records

    def get_input_file(self, *, file_id: str, user_id: str) -> AgentInputFileRecord:
        payload = self._files.get(file_id)
        if payload is None or payload["user_id"] != user_id:
            raise AgentResourceNotFound(file_id)
        return AgentInputFileRecord.model_validate(deepcopy(payload))

    def get_latest_active_bundle(self, *, thread_id: str, user_id: str, before: datetime) -> AgentInputBundleRecord | None:
        self.get_thread(thread_id=thread_id, user_id=user_id)
        candidates = [
            AgentInputBundleRecord.model_validate(deepcopy(payload))
            for payload in self._bundles.values()
            if (payload["thread_id"] == thread_id
                and payload["user_id"] == user_id
                and payload["status"] == "active")
        ]
        candidates = [b for b in candidates if b.created_at < before and b.expires_at > before]
        if not candidates:
            return None
        candidates.sort(key=lambda item: item.created_at, reverse=True)
        return candidates[0]

    def create_job_wait(self, wait: AgentJobWaitRecord) -> AgentJobWaitRecord:
        self.get_thread(thread_id=wait.thread_id, user_id=wait.user_id)
        for payload in self._job_waits.values():
            if (
                payload["job_id"], payload["thread_id"], payload["user_id"]
            ) == (wait.job_id, wait.thread_id, wait.user_id):
                existing = AgentJobWaitRecord.model_validate(deepcopy(payload))
                if existing.model_dump(mode="json") != wait.model_dump(mode="json"):
                    raise ValueError("job wait already exists with different ownership")
                return existing
        if wait.wait_id in self._job_waits:
            raise ValueError("job wait id already exists")
        self._job_waits[wait.wait_id] = wait.model_dump(mode="json")
        return wait.model_copy(deep=True)

    def get_job_wait(self, *, job_id: str, user_id: str) -> AgentJobWaitRecord:
        for payload in self._job_waits.values():
            if payload["job_id"] == job_id and payload["user_id"] == user_id:
                return AgentJobWaitRecord.model_validate(deepcopy(payload))
        raise AgentResourceNotFound(job_id)

    def list_job_waits(
        self, *, thread_id: str, user_id: str, limit: int = 100,
    ) -> list[AgentJobWaitRecord]:
        self.get_thread(thread_id=thread_id, user_id=user_id)
        bounded = max(1, min(limit, 100))
        records = [
            AgentJobWaitRecord.model_validate(deepcopy(payload))
            for payload in self._job_waits.values()
            if payload["thread_id"] == thread_id and payload["user_id"] == user_id
        ]
        records.sort(key=lambda item: (item.updated_at, item.wait_id))
        return records[-bounded:]

    def cancel_job_wait(
        self, *, job_id: str, user_id: str, now: datetime,
    ) -> AgentJobWaitRecord:
        wait = self.get_job_wait(job_id=job_id, user_id=user_id)
        if wait.status in {
            AgentJobWaitStatus.COMPLETED,
            AgentJobWaitStatus.FAILED,
            AgentJobWaitStatus.CANCELLED,
            AgentJobWaitStatus.EXPIRED,
        }:
            return wait
        updated = wait.model_copy(update={
            "status": AgentJobWaitStatus.CANCELLED,
            "updated_at": now,
        })
        self._job_waits[wait.wait_id] = updated.model_dump(mode="json")
        return updated

    def save_job_completion_event(self, event: AgentJobCompletionEvent) -> None:
        self.get_thread(thread_id=event.thread_id, user_id=event.user_id)
        current = self._job_events.get(event.event_id)
        if current is not None:
            current_event = AgentJobCompletionEvent.model_validate(deepcopy(current))
            immutable = {"published_at", "delivery_attempts", "last_error"}
            if current_event.model_dump(mode="json", exclude=immutable) != event.model_dump(
                mode="json", exclude=immutable
            ):
                raise ValueError("job completion event already exists with different payload")
            return
        self._job_events[event.event_id] = event.model_dump(mode="json")

    def list_pending_job_events(self, *, limit: int = 20) -> list[AgentJobCompletionEvent]:
        bounded = max(1, min(limit, 100))
        records = [
            AgentJobCompletionEvent.model_validate(deepcopy(payload))
            for payload in self._job_events.values()
            if payload.get("published_at") is None
        ]
        records.sort(key=lambda item: (item.occurred_at, item.event_id))
        return records[:bounded]

    def prepare_job_continuation(
        self, event: AgentJobCompletionEvent, *, now: datetime,
    ) -> AgentTurnRecord | None:
        stored_payload = self._job_events.get(event.event_id)
        if stored_payload is None:
            raise AgentResourceNotFound(event.event_id)
        stored = AgentJobCompletionEvent.model_validate(deepcopy(stored_payload))
        if stored.model_dump(mode="json") != event.model_dump(mode="json"):
            raise ValueError("job completion event payload changed")
        wait = self.get_job_wait(job_id=event.job_id, user_id=event.user_id)
        if (wait.thread_id, wait.turn_id, wait.run_id, wait.trace_id) != (
            event.thread_id, event.turn_id, event.run_id, event.trace_id,
        ):
            raise AgentResourceNotFound(event.job_id)
        if wait.status in {
            AgentJobWaitStatus.COMPLETED,
            AgentJobWaitStatus.FAILED,
            AgentJobWaitStatus.CANCELLED,
            AgentJobWaitStatus.EXPIRED,
        }:
            return None
        if wait.status is AgentJobWaitStatus.RESUME_QUEUED:
            if wait.continuation_turn_id is None:
                raise ValueError("job wait is queued without a continuation turn")
            return self.get_turn(
                turn_id=wait.continuation_turn_id,
                user_id=event.user_id,
            )
        turn = _continuation_turn(event, now)
        queued, _created = self.enqueue_turn(message=None, turn=turn)
        updated = wait.model_copy(update={
            "status": AgentJobWaitStatus.RESUME_QUEUED,
            "continuation_turn_id": queued.turn_id,
            "updated_at": now,
        })
        self._job_waits[wait.wait_id] = updated.model_dump(mode="json")
        return queued

    def mark_job_event_published(self, *, event_id: str, now: datetime) -> None:
        payload = self._job_events.get(event_id)
        if payload is None:
            raise AgentResourceNotFound(event_id)
        payload["published_at"] = now.isoformat()
        payload["delivery_attempts"] = int(payload.get("delivery_attempts") or 0) + 1
        payload["last_error"] = None

    def mark_job_event_failed(self, *, event_id: str, error: str, now: datetime) -> None:
        payload = self._job_events.get(event_id)
        if payload is None:
            raise AgentResourceNotFound(event_id)
        payload["delivery_attempts"] = int(payload.get("delivery_attempts") or 0) + 1
        payload["last_error"] = error[:500]
        payload["occurred_at"] = payload.get("occurred_at") or now.isoformat()

    def complete_job_wait(
        self, *, job_id: str, user_id: str, continuation_turn_id: str,
        status: AgentJobWaitStatus, now: datetime,
    ) -> AgentJobWaitRecord:
        if status not in {
            AgentJobWaitStatus.COMPLETED,
            AgentJobWaitStatus.FAILED,
            AgentJobWaitStatus.CANCELLED,
            AgentJobWaitStatus.EXPIRED,
        }:
            raise ValueError("job wait completion requires a terminal status")
        wait = self.get_job_wait(job_id=job_id, user_id=user_id)
        if wait.continuation_turn_id not in {None, continuation_turn_id}:
            raise TurnConflict(continuation_turn_id)
        if wait.status in {
            AgentJobWaitStatus.COMPLETED,
            AgentJobWaitStatus.FAILED,
            AgentJobWaitStatus.CANCELLED,
            AgentJobWaitStatus.EXPIRED,
        }:
            return wait
        updated = wait.model_copy(update={
            "status": status,
            "continuation_turn_id": continuation_turn_id,
            "updated_at": now,
        })
        self._job_waits[wait.wait_id] = updated.model_dump(mode="json")
        return updated


class PostgresAgentProductStore:
    """产品对话表的 PostgreSQL repository；所有资源读取都绑定用户。"""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def record_trace_event(self, event: AgentTraceEvent) -> None:
        Jsonb = _jsonb_type()
        with self._connect() as conn:
            conn.execute(
                """
                insert into agent_trace_events (
                    event_id, trace_id, thread_id, turn_id, run_id, user_id,
                    event_type, component, name, schema_version, graph_version,
                    prompt_version, prompt_hash, model_provider, model_name,
                    tool_name, tool_schema_hash, job_id, outcome, latency_ms,
                    prompt_tokens, completion_tokens, total_tokens, cached_tokens,
                    usage_status, retry_count, error_code, created_at
                ) values (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s
                ) on conflict (event_id) do nothing
                """,
                (
                    event.event_id, event.trace_id, event.thread_id, event.turn_id,
                    event.run_id, event.user_id, event.event_type, event.component,
                    event.name, event.schema_version, event.graph_version,
                    event.prompt_version, event.prompt_hash, event.model_provider,
                    event.model_name, event.tool_name, event.tool_schema_hash,
                    event.job_id, event.outcome, event.latency_ms, event.prompt_tokens,
                    event.completion_tokens, event.total_tokens, event.cached_tokens,
                    event.usage_status, event.retry_count, event.error_code,
                    event.created_at,
                ),
            )

    def list_trace_events(
        self, *, trace_id: str, user_id: str, limit: int = 100,
    ) -> list[AgentTraceEvent]:
        bounded = max(1, min(limit, 500))
        with self._connect() as conn:
            rows = conn.execute(
                """
                select event_id, trace_id, thread_id, turn_id, run_id, user_id,
                       event_type, component, name, schema_version, graph_version,
                       prompt_version, prompt_hash, model_provider, model_name,
                       tool_name, tool_schema_hash, job_id, outcome, latency_ms,
                       prompt_tokens, completion_tokens, total_tokens, cached_tokens,
                       usage_status, retry_count, error_code, created_at
                from agent_trace_events
                where trace_id = %s and user_id = %s
                order by created_at desc, event_id desc
                limit %s
                """,
                (trace_id, user_id, bounded),
            ).fetchall()
        return [_trace_event_from_row(row) for row in reversed(rows)]

    def get_feedback_target(
        self, *, thread_id: str, message_id: str, user_id: str,
    ) -> tuple[AgentMessageRecord, AgentTurnRecord, AgentMessageRecord | None]:
        with self._connect() as conn:
            row = conn.execute(
                """
                select
                    m.message_id, m.thread_id, m.run_id, m.trace_id, m.user_id,
                    m.role, m.blocks, m.created_at,
                    t.turn_id, t.thread_id, t.run_id, t.user_id, t.trace_id,
                    t.idempotency_key, t.request_hash, t.status, t.attempt,
                    t.error_code, t.created_at, t.updated_at, t.started_at,
                    t.completed_at
                from agent_messages m
                join agent_turns t
                  on m.message_id = 'assistant-' || t.turn_id
                 and m.thread_id = t.thread_id
                 and m.run_id = t.run_id
                 and m.trace_id = t.trace_id
                 and m.user_id = t.user_id
                where m.message_id = %s
                  and m.thread_id = %s
                  and m.user_id = %s
                  and m.role = 'assistant'
                """,
                (message_id, thread_id, user_id),
            ).fetchone()
            if row is None:
                raise AgentResourceNotFound(message_id)
            assistant = _message_from_row(row[:8])
            turn = _turn_from_row(row[8:])
            user_row = conn.execute(
                """
                select message_id, thread_id, run_id, trace_id, user_id, role, blocks, created_at
                from agent_messages
                where message_id = 'user-' || %s
                  and thread_id = %s and run_id = %s and trace_id = %s
                  and user_id = %s and role = 'user'
                """,
                (turn.turn_id, thread_id, turn.run_id, turn.trace_id, user_id),
            ).fetchone()
        return assistant, turn, _message_from_row(user_row) if user_row is not None else None

    def save_feedback(self, feedback: AgentFeedbackRecord) -> AgentFeedbackRecord:
        self._validate_feedback_record(feedback)
        with self._connect() as conn:
            row = conn.execute(
                """
                insert into agent_feedback (
                    feedback_id, thread_id, turn_id, message_id, trace_id, user_id,
                    rating, failure_category, correction_text, created_at, updated_at
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (message_id, user_id) do update set
                    rating = excluded.rating,
                    failure_category = excluded.failure_category,
                    correction_text = excluded.correction_text,
                    updated_at = excluded.updated_at
                returning feedback_id, thread_id, turn_id, message_id, trace_id, user_id,
                          rating, failure_category, correction_text, created_at, updated_at
                """,
                (
                    feedback.feedback_id, feedback.thread_id, feedback.turn_id,
                    feedback.message_id, feedback.trace_id, feedback.user_id,
                    feedback.rating.value,
                    feedback.failure_category.value if feedback.failure_category else None,
                    feedback.correction_text, feedback.created_at, feedback.updated_at,
                ),
            ).fetchone()
        return _feedback_from_row(row)

    def list_feedback(
        self, *, thread_id: str, user_id: str, limit: int = 100,
    ) -> list[AgentFeedbackRecord]:
        self.get_thread(thread_id=thread_id, user_id=user_id)
        bounded = max(1, min(limit, 100))
        with self._connect() as conn:
            rows = conn.execute(
                """
                select feedback_id, thread_id, turn_id, message_id, trace_id, user_id,
                       rating, failure_category, correction_text, created_at, updated_at
                from agent_feedback
                where thread_id = %s and user_id = %s
                order by updated_at desc, feedback_id desc limit %s
                """,
                (thread_id, user_id, bounded),
            ).fetchall()
        return [_feedback_from_row(row) for row in rows]

    def save_eval_candidate(
        self, candidate: AgentEvalCandidateRecord,
    ) -> AgentEvalCandidateRecord:
        if candidate.status.value != "pending_review":
            raise ValueError("candidate review status is controlled outside the public feedback flow")
        Jsonb = _jsonb_type()
        with self._connect() as conn:
            feedback_row = conn.execute(
                """
                select thread_id, turn_id, message_id, trace_id, user_id, rating, failure_category
                from agent_feedback where feedback_id = %s
                """,
                (candidate.feedback_id,),
            ).fetchone()
            if feedback_row is None or tuple(feedback_row) != (
                candidate.thread_id, candidate.turn_id, candidate.message_id,
                candidate.trace_id, candidate.user_id, candidate.rating.value,
                candidate.failure_category.value if candidate.failure_category else None,
            ):
                raise AgentResourceNotFound(candidate.feedback_id)
            row = conn.execute(
                """
                insert into agent_eval_candidates (
                    candidate_id, feedback_id, thread_id, turn_id, message_id, trace_id,
                    user_id, status, rating, failure_category, user_message_summary,
                    assistant_message_summary, correction_summary, trace_summary,
                    created_at, updated_at
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (feedback_id) do update set
                    status = excluded.status, rating = excluded.rating,
                    failure_category = excluded.failure_category,
                    user_message_summary = excluded.user_message_summary,
                    assistant_message_summary = excluded.assistant_message_summary,
                    correction_summary = excluded.correction_summary,
                    trace_summary = excluded.trace_summary, updated_at = excluded.updated_at
                returning candidate_id, feedback_id, thread_id, turn_id, message_id, trace_id,
                          user_id, status, rating, failure_category, user_message_summary,
                          assistant_message_summary, correction_summary, trace_summary,
                          created_at, updated_at
                """,
                (
                    candidate.candidate_id, candidate.feedback_id, candidate.thread_id,
                    candidate.turn_id, candidate.message_id, candidate.trace_id,
                    candidate.user_id, candidate.status.value, candidate.rating.value,
                    candidate.failure_category.value if candidate.failure_category else None,
                    candidate.user_message_summary, candidate.assistant_message_summary,
                    candidate.correction_summary, Jsonb(candidate.trace_summary.model_dump(mode="json")),
                    candidate.created_at, candidate.updated_at,
                ),
            ).fetchone()
        return _eval_candidate_from_row(row)

    def get_eval_candidate(
        self, *, candidate_id: str, user_id: str,
    ) -> AgentEvalCandidateRecord:
        with self._connect() as conn:
            row = conn.execute(
                """
                select candidate_id, feedback_id, thread_id, turn_id, message_id, trace_id,
                       user_id, status, rating, failure_category, user_message_summary,
                       assistant_message_summary, correction_summary, trace_summary,
                       created_at, updated_at
                from agent_eval_candidates
                where candidate_id = %s and user_id = %s
                """,
                (candidate_id, user_id),
            ).fetchone()
        if row is None:
            raise AgentResourceNotFound(candidate_id)
        return _eval_candidate_from_row(row)

    def list_eval_candidates_for_review(
        self, *, limit: int = 100,
    ) -> list[AgentEvalCandidateRecord]:
        bounded = max(1, min(limit, 1000))
        with self._connect() as conn:
            rows = conn.execute(
                """
                select candidate_id, feedback_id, thread_id, turn_id, message_id, trace_id,
                       user_id, status, rating, failure_category, user_message_summary,
                       assistant_message_summary, correction_summary, trace_summary,
                       created_at, updated_at
                from agent_eval_candidates
                where status = 'pending_review'
                order by updated_at, candidate_id limit %s
                """,
                (bounded,),
            ).fetchall()
        return [_eval_candidate_from_row(row) for row in rows]

    def delete_eval_candidate_for_feedback(
        self, *, feedback_id: str, user_id: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                delete from agent_eval_candidates
                where feedback_id = %s and user_id = %s
                """,
                (feedback_id, user_id),
            )

    def _validate_feedback_record(self, feedback: AgentFeedbackRecord) -> None:
        _assistant, turn, _user_message = self.get_feedback_target(
            thread_id=feedback.thread_id,
            message_id=feedback.message_id,
            user_id=feedback.user_id,
        )
        if (turn.turn_id, turn.trace_id) != (feedback.turn_id, feedback.trace_id):
            raise AgentResourceNotFound(feedback.message_id)

    def save_thread(self, thread: AgentThreadRecord) -> None:
        with self._connect() as conn:
            row = conn.execute(
                """
                insert into agent_threads (
                    thread_id, user_id, title, current_run_id, status, version, created_at, updated_at
                ) values (%s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (thread_id) do update set
                    title = excluded.title,
                    current_run_id = excluded.current_run_id,
                    status = excluded.status,
                    version = excluded.version,
                    updated_at = excluded.updated_at
                where agent_threads.user_id = excluded.user_id
                returning thread_id
                """,
                (
                    thread.thread_id,
                    thread.user_id,
                    thread.title,
                    thread.current_run_id,
                    thread.status.value,
                    thread.version,
                    thread.created_at,
                    thread.updated_at,
                ),
            ).fetchone()
        if row is None:
            raise AgentResourceNotFound(thread.thread_id)

    def get_thread(self, *, thread_id: str, user_id: str) -> AgentThreadRecord:
        with self._connect() as conn:
            row = conn.execute(
                """
                select thread_id, user_id, title, current_run_id, status, version, created_at, updated_at
                from agent_threads where thread_id = %s and user_id = %s
                """,
                (thread_id, user_id),
            ).fetchone()
        if row is None:
            raise AgentResourceNotFound(thread_id)
        return _thread_from_row(row)

    def list_threads(self, *, user_id: str, limit: int = 100) -> list[AgentThreadRecord]:
        bounded = max(1, min(limit, 100))
        with self._connect() as conn:
            rows = conn.execute(
                """
                select thread_id, user_id, title, current_run_id, status, version, created_at, updated_at
                from agent_threads where user_id = %s
                order by updated_at desc, thread_id desc limit %s
                """,
                (user_id, bounded),
            ).fetchall()
        return [_thread_from_row(row) for row in rows]

    def delete_thread(self, *, thread_id: str, user_id: str) -> list[AgentInputFileRecord]:
        with self._connect() as conn:
            owned = conn.execute(
                "select 1 from agent_threads where thread_id = %s and user_id = %s for update",
                (thread_id, user_id),
            ).fetchone()
            if owned is None:
                raise AgentResourceNotFound(thread_id)
            rows = conn.execute(
                """
                select f.file_id, f.bundle_id, f.user_id, f.field, f.filename, f.storage_key,
                       f.checksum, f.content_type, f.size_bytes, f.created_at
                from agent_input_files f
                join agent_input_bundles b on b.bundle_id = f.bundle_id and b.user_id = f.user_id
                where b.thread_id = %s and b.user_id = %s
                """,
                (thread_id, user_id),
            ).fetchall()
            conn.execute("delete from agent_input_files where user_id = %s and bundle_id in (select bundle_id from agent_input_bundles where thread_id = %s and user_id = %s)", (user_id, thread_id, user_id))
            conn.execute("delete from agent_input_bundles where thread_id = %s and user_id = %s", (thread_id, user_id))
            conn.execute("delete from agent_messages where thread_id = %s and user_id = %s", (thread_id, user_id))
            conn.execute("delete from agent_turns where thread_id = %s and user_id = %s", (thread_id, user_id))
            conn.execute("delete from agent_trace_events where thread_id = %s and user_id = %s", (thread_id, user_id))
            conn.execute("delete from agent_threads where thread_id = %s and user_id = %s", (thread_id, user_id))
        return [_input_file_from_row(row) for row in rows]

    def append_message(self, message: AgentMessageRecord) -> None:
        self.get_thread(thread_id=message.thread_id, user_id=message.user_id)
        Jsonb = _jsonb_type()
        with self._connect() as conn:
            conn.execute(
                """
                insert into agent_messages (
                    message_id, thread_id, run_id, trace_id, user_id, role, blocks, created_at
                ) values (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    message.message_id,
                    message.thread_id,
                    message.run_id,
                    message.trace_id,
                    message.user_id,
                    message.role.value,
                    Jsonb([block.model_dump(mode="json") for block in message.blocks]),
                    message.created_at,
                ),
            )

    def list_messages(self, *, thread_id: str, user_id: str, limit: int = 100) -> list[AgentMessageRecord]:
        self.get_thread(thread_id=thread_id, user_id=user_id)
        bounded = max(1, min(limit, 100))
        with self._connect() as conn:
            rows = conn.execute(
                """
                select message_id, thread_id, run_id, trace_id, user_id, role, blocks, created_at
                from agent_messages
                where thread_id = %s and user_id = %s
                order by created_at desc, message_id desc limit %s
                """,
                (thread_id, user_id, bounded),
            ).fetchall()
        return [_message_from_row(row) for row in reversed(rows)]

    def create_turn(self, turn: AgentTurnRecord) -> AgentTurnRecord:
        return self.enqueue_turn(message=None, turn=turn)[0]

    def enqueue_turn(
        self,
        *,
        message: AgentMessageRecord | None,
        turn: AgentTurnRecord,
    ) -> tuple[AgentTurnRecord, bool]:
        Jsonb = _jsonb_type()
        try:
            with self._connect() as conn:
                thread_row = conn.execute(
                    """
                    select current_run_id from agent_threads
                    where thread_id = %s and user_id = %s
                    for update
                    """,
                    (turn.thread_id, turn.user_id),
                ).fetchone()
                if thread_row is None or thread_row[0] != turn.run_id:
                    raise AgentResourceNotFound(turn.thread_id)
                existing_row = conn.execute(
                    """
                    select turn_id, thread_id, run_id, user_id, trace_id, idempotency_key, request_hash,
                           status, attempt, error_code,
                           created_at, updated_at, started_at, completed_at
                    from agent_turns where user_id = %s and idempotency_key = %s
                    """,
                    (turn.user_id, turn.idempotency_key),
                ).fetchone()
                if existing_row is not None:
                    existing = _turn_from_row(existing_row)
                    if (
                        existing.request_hash != turn.request_hash
                        or existing.thread_id != turn.thread_id
                        or existing.run_id != turn.run_id
                    ):
                        raise IdempotencyConflict(turn.idempotency_key)
                    return existing, False
                row = conn.execute(
                    """
                    insert into agent_turns (
                        turn_id, thread_id, run_id, user_id, trace_id, idempotency_key, request_hash,
                        status, attempt, error_code,
                        created_at, updated_at, started_at, completed_at
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (user_id, idempotency_key) do nothing
                    returning turn_id
                    """,
                    _turn_values(turn),
                ).fetchone()
                if row is None:
                    existing_row = conn.execute(
                        """
                        select turn_id, thread_id, run_id, user_id, trace_id, idempotency_key, request_hash,
                               status, attempt, error_code,
                               created_at, updated_at, started_at, completed_at
                        from agent_turns where user_id = %s and idempotency_key = %s
                        """,
                        (turn.user_id, turn.idempotency_key),
                    ).fetchone()
                    if existing_row is None:
                        raise RuntimeError("turn insert returned no record")
                    existing = _turn_from_row(existing_row)
                    if (
                        existing.request_hash != turn.request_hash
                        or existing.thread_id != turn.thread_id
                        or existing.run_id != turn.run_id
                    ):
                        raise IdempotencyConflict(turn.idempotency_key)
                    return existing, False
                if message is not None:
                    if (message.thread_id, message.run_id, message.user_id) != (
                        turn.thread_id, turn.run_id, turn.user_id,
                    ):
                        raise ValueError("turn message does not match turn ownership")
                    conn.execute(
                        """
                        insert into agent_messages (
                            message_id, thread_id, run_id, trace_id, user_id, role, blocks, created_at
                        ) values (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            message.message_id,
                            message.thread_id,
                            message.run_id,
                            message.trace_id,
                            message.user_id,
                            message.role.value,
                            Jsonb([block.model_dump(mode="json") for block in message.blocks]),
                            message.created_at,
                        ),
                    )
                return turn.model_copy(deep=True), True
        except Exception as exc:
            if _constraint_name(exc) == "agent_turns_one_active_per_thread_idx":
                raise ActiveTurnConflict(turn.thread_id) from exc
            raise

    def get_turn(self, *, turn_id: str, user_id: str) -> AgentTurnRecord:
        with self._connect() as conn:
            row = conn.execute(
                """
                select turn_id, thread_id, run_id, user_id, trace_id, idempotency_key, request_hash,
                       status, attempt, error_code,
                       created_at, updated_at, started_at, completed_at
                from agent_turns where turn_id = %s and user_id = %s
                """,
                (turn_id, user_id),
            ).fetchone()
        if row is None:
            raise AgentResourceNotFound(turn_id)
        return _turn_from_row(row)

    def claim_turn(self, *, turn_id: str, user_id: str, now: datetime) -> AgentTurnRecord:
        with self._connect() as conn:
            row = conn.execute(
                """
                update agent_turns set status = 'running', attempt = attempt + 1,
                    started_at = %s, updated_at = %s, error_code = null
                where turn_id = %s and user_id = %s and status in ('queued', 'running')
                returning turn_id, thread_id, run_id, user_id, trace_id, idempotency_key,
                          request_hash, status, attempt, error_code,
                          created_at, updated_at, started_at, completed_at
                """,
                (now, now, turn_id, user_id),
            ).fetchone()
        return _turn_from_row(row) if row is not None else self.get_turn(
            turn_id=turn_id, user_id=user_id
        )

    def queue_turn(self, *, turn_id: str, user_id: str, now: datetime) -> AgentTurnRecord:
        with self._connect() as conn:
            row = conn.execute(
                """
                update agent_turns set status = 'queued', error_code = null,
                    started_at = null, completed_at = null, updated_at = %s
                where turn_id = %s and user_id = %s and status = 'running'
                returning turn_id, thread_id, run_id, user_id, trace_id, idempotency_key,
                          request_hash, status, attempt, error_code,
                          created_at, updated_at, started_at, completed_at
                """,
                (now, turn_id, user_id),
            ).fetchone()
        if row is None:
            raise TurnConflict(turn_id)
        return _turn_from_row(row)

    def list_turns(self, *, thread_id: str, user_id: str, limit: int = 100) -> list[AgentTurnRecord]:
        self.get_thread(thread_id=thread_id, user_id=user_id)
        bounded = max(1, min(limit, 100))
        with self._connect() as conn:
            rows = conn.execute(
                """
                select turn_id, thread_id, run_id, user_id, trace_id, idempotency_key, request_hash,
                       status, attempt, error_code,
                       created_at, updated_at, started_at, completed_at
                from agent_turns
                where thread_id = %s and user_id = %s
                order by created_at desc, turn_id desc limit %s
                """,
                (thread_id, user_id, bounded),
            ).fetchall()
        return [_turn_from_row(row) for row in reversed(rows)]

    def finish_turn(
        self, *, turn_id: str, user_id: str, status: AgentTurnStatus, now: datetime,
        message: AgentMessageRecord | None = None, error_code: str | None = None,
    ) -> AgentTurnRecord:
        if status not in {AgentTurnStatus.COMPLETED, AgentTurnStatus.FAILED}:
            raise ValueError("turn may only finish as completed or failed")
        Jsonb = _jsonb_type()
        with self._connect() as conn:
            row = conn.execute(
                """
                update agent_turns set status = %s, error_code = %s,
                    completed_at = %s, updated_at = %s
                where turn_id = %s and user_id = %s
                  and status = 'running'
                returning turn_id, thread_id, run_id, user_id, trace_id, idempotency_key,
                          request_hash, status, attempt,
                          error_code, created_at, updated_at, started_at, completed_at
                """,
                (status.value, error_code, now, now, turn_id, user_id),
            ).fetchone()
            if row is None:
                raise TurnConflict(turn_id)
            turn = _turn_from_row(row)
            if message is not None:
                if (message.thread_id, message.run_id, message.user_id) != (
                    turn.thread_id, turn.run_id, turn.user_id,
                ):
                    raise ValueError("turn message does not match ownership")
                conn.execute(
                    """
                    insert into agent_messages (
                        message_id, thread_id, run_id, trace_id, user_id, role, blocks, created_at
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (message.message_id, message.thread_id, message.run_id, message.trace_id,
                     message.user_id, message.role.value,
                     Jsonb([block.model_dump(mode="json") for block in message.blocks]),
                     message.created_at),
                )
        return turn

    def cancel_turn(self, *, turn_id: str, user_id: str, now: datetime, error_code: str) -> AgentTurnRecord:
        with self._connect() as conn:
            row = conn.execute(
                """
                update agent_turns set status = 'cancelled', error_code = %s,
                    completed_at = %s, updated_at = %s
                where turn_id = %s and user_id = %s
                  and status in ('queued', 'running')
                returning turn_id, thread_id, run_id, user_id, trace_id, idempotency_key,
                          request_hash, status, attempt, error_code,
                          created_at, updated_at, started_at, completed_at
                """,
                (error_code, now, now, turn_id, user_id),
            ).fetchone()
        if row is None:
            raise TurnConflict(turn_id)
        return _turn_from_row(row)

    def save_input_bundle(self, bundle: AgentInputBundleRecord) -> None:
        self.get_thread(thread_id=bundle.thread_id, user_id=bundle.user_id)
        with self._connect() as conn:
            row = conn.execute(
                """
                insert into agent_input_bundles (
                    bundle_id, thread_id, user_id, status, expires_at, created_at
                ) values (%s, %s, %s, %s, %s, %s)
                on conflict (bundle_id) do update set
                    status = excluded.status,
                    expires_at = excluded.expires_at
                where agent_input_bundles.user_id = excluded.user_id
                returning bundle_id
                """,
                (
                    bundle.bundle_id,
                    bundle.thread_id,
                    bundle.user_id,
                    bundle.status.value,
                    bundle.expires_at,
                    bundle.created_at,
                ),
            ).fetchone()
        if row is None:
            raise AgentResourceNotFound(bundle.bundle_id)

    def save_input_bundle_with_files(self, *, bundle: AgentInputBundleRecord,
                                     files: list[AgentInputFileRecord]) -> None:
        if len(files) > 6 or any(
            (item.bundle_id, item.user_id) != (bundle.bundle_id, bundle.user_id)
            for item in files
        ):
            raise ValueError("input files do not match bundle ownership")
        if len({item.field for item in files}) != len(files):
            raise ValueError("input bundle fields must be unique")
        with self._connect() as conn:
            owned = conn.execute(
                "select thread_id from agent_threads where thread_id = %s and user_id = %s",
                (bundle.thread_id, bundle.user_id),
            ).fetchone()
            if owned is None:
                raise AgentResourceNotFound(bundle.thread_id)
            conn.execute(
                """
                insert into agent_input_bundles (
                    bundle_id, thread_id, user_id, status, expires_at, created_at
                ) values (%s, %s, %s, %s, %s, %s)
                """,
                (
                    bundle.bundle_id,
                    bundle.thread_id,
                    bundle.user_id,
                    bundle.status.value,
                    bundle.expires_at,
                    bundle.created_at,
                ),
            )
            for item in files:
                conn.execute(
                    """
                    insert into agent_input_files (
                        file_id, bundle_id, user_id, field, filename, storage_key,
                        checksum, content_type, size_bytes, created_at
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        item.file_id,
                        item.bundle_id,
                        item.user_id,
                        item.field,
                        item.filename,
                        item.storage_key,
                        item.checksum,
                        item.content_type,
                        item.size_bytes,
                        item.created_at,
                    ),
                )

    def get_input_bundle(self, *, bundle_id: str, user_id: str) -> AgentInputBundleRecord:
        with self._connect() as conn:
            row = conn.execute(
                """
                select bundle_id, thread_id, user_id, status, expires_at, created_at
                from agent_input_bundles where bundle_id = %s and user_id = %s
                """,
                (bundle_id, user_id),
            ).fetchone()
        if row is None:
            raise AgentResourceNotFound(bundle_id)
        return AgentInputBundleRecord.model_validate(dict(zip(
            ("bundle_id", "thread_id", "user_id", "status", "expires_at", "created_at"), row,
        )))

    def append_input_file(self, item: AgentInputFileRecord) -> None:
        self.get_input_bundle(bundle_id=item.bundle_id, user_id=item.user_id)
        with self._connect() as conn:
            conn.execute(
                """
                insert into agent_input_files (
                    file_id, bundle_id, user_id, field, filename, storage_key,
                    checksum, content_type, size_bytes, created_at
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    item.file_id,
                    item.bundle_id,
                    item.user_id,
                    item.field,
                    item.filename,
                    item.storage_key,
                    item.checksum,
                    item.content_type,
                    item.size_bytes,
                    item.created_at,
                ),
            )

    def list_input_files(self, *, bundle_id: str, user_id: str) -> list[AgentInputFileRecord]:
        self.get_input_bundle(bundle_id=bundle_id, user_id=user_id)
        with self._connect() as conn:
            rows = conn.execute(
                """
                select file_id, bundle_id, user_id, field, filename, storage_key,
                       checksum, content_type, size_bytes, created_at
                from agent_input_files
                where bundle_id = %s and user_id = %s
                order by created_at, file_id
                """,
                (bundle_id, user_id),
            ).fetchall()
        fields = (
            "file_id", "bundle_id", "user_id", "field", "filename", "storage_key",
            "checksum", "content_type", "size_bytes", "created_at",
        )
        return [AgentInputFileRecord.model_validate(dict(zip(fields, row))) for row in rows]

    def get_input_file(self, *, file_id: str, user_id: str) -> AgentInputFileRecord:
        with self._connect() as conn:
            row = conn.execute(
                """
                select file_id, bundle_id, user_id, field, filename, storage_key,
                       checksum, content_type, size_bytes, created_at
                from agent_input_files where file_id = %s and user_id = %s
                """,
                (file_id, user_id),
            ).fetchone()
        if row is None:
            raise AgentResourceNotFound(file_id)
        fields = (
            "file_id", "bundle_id", "user_id", "field", "filename", "storage_key",
            "checksum", "content_type", "size_bytes", "created_at",
        )
        return AgentInputFileRecord.model_validate(dict(zip(fields, row)))

    def get_latest_active_bundle(self, *, thread_id: str, user_id: str, before: datetime) -> AgentInputBundleRecord | None:
        self.get_thread(thread_id=thread_id, user_id=user_id)
        with self._connect() as conn:
            row = conn.execute(
                """
                select bundle_id, thread_id, user_id, status, expires_at, created_at
                from agent_input_bundles
                where thread_id = %s and user_id = %s
                  and status = 'active'
                  and created_at < %s
                  and expires_at > %s
                order by created_at desc
                limit 1
                """,
                (thread_id, user_id, before, before),
            ).fetchone()
        if row is None:
            return None
        return AgentInputBundleRecord.model_validate(dict(zip(
            ("bundle_id", "thread_id", "user_id", "status", "expires_at", "created_at"), row,
        )))

    def create_job_wait(self, wait: AgentJobWaitRecord) -> AgentJobWaitRecord:
        self.get_thread(thread_id=wait.thread_id, user_id=wait.user_id)
        with self._connect() as conn:
            owned = conn.execute(
                "select 1 from jobs where id = %s and owner_id = %s",
                (wait.job_id, wait.user_id),
            ).fetchone()
            if owned is None:
                raise AgentResourceNotFound(wait.job_id)
            turn = conn.execute(
                """
                select 1 from agent_turns
                where turn_id = %s and user_id = %s and thread_id = %s and run_id = %s
                """,
                (wait.turn_id, wait.user_id, wait.thread_id, wait.run_id),
            ).fetchone()
            if turn is None:
                raise AgentResourceNotFound(wait.turn_id)
            row = conn.execute(
                """
                insert into agent_job_waits (
                    wait_id, thread_id, user_id, turn_id, run_id, trace_id, job_id,
                    status, continuation_turn_id, expires_at, created_at, updated_at
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (job_id, thread_id, user_id) do nothing
                returning wait_id, thread_id, user_id, turn_id, run_id, trace_id, job_id,
                          status, continuation_turn_id, expires_at, created_at, updated_at
                """,
                (
                    wait.wait_id, wait.thread_id, wait.user_id, wait.turn_id,
                    wait.run_id, wait.trace_id, wait.job_id, wait.status.value,
                    wait.continuation_turn_id, wait.expires_at, wait.created_at,
                    wait.updated_at,
                ),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    """
                    select wait_id, thread_id, user_id, turn_id, run_id, trace_id, job_id,
                           status, continuation_turn_id, expires_at, created_at, updated_at
                    from agent_job_waits
                    where job_id = %s and thread_id = %s and user_id = %s
                    """,
                    (wait.job_id, wait.thread_id, wait.user_id),
                ).fetchone()
            if row is None:
                raise RuntimeError("job wait insert returned no record")
        return _job_wait_from_row(row)

    def get_job_wait(self, *, job_id: str, user_id: str) -> AgentJobWaitRecord:
        with self._connect() as conn:
            row = conn.execute(
                """
                select wait_id, thread_id, user_id, turn_id, run_id, trace_id, job_id,
                       status, continuation_turn_id, expires_at, created_at, updated_at
                from agent_job_waits where job_id = %s and user_id = %s
                """,
                (job_id, user_id),
            ).fetchone()
        if row is None:
            raise AgentResourceNotFound(job_id)
        return _job_wait_from_row(row)

    def list_job_waits(
        self, *, thread_id: str, user_id: str, limit: int = 100,
    ) -> list[AgentJobWaitRecord]:
        self.get_thread(thread_id=thread_id, user_id=user_id)
        bounded = max(1, min(limit, 100))
        with self._connect() as conn:
            rows = conn.execute(
                """
                select wait_id, thread_id, user_id, turn_id, run_id, trace_id, job_id,
                       status, continuation_turn_id, expires_at, created_at, updated_at
                from agent_job_waits
                where thread_id = %s and user_id = %s
                order by updated_at desc, wait_id desc
                limit %s
                """,
                (thread_id, user_id, bounded),
            ).fetchall()
        return [_job_wait_from_row(row) for row in reversed(rows)]

    def cancel_job_wait(
        self, *, job_id: str, user_id: str, now: datetime,
    ) -> AgentJobWaitRecord:
        with self._connect() as conn:
            row = conn.execute(
                """
                update agent_job_waits
                set status = case when status in
                    ('completed', 'failed', 'cancelled', 'expired')
                    then status else 'cancelled' end,
                    updated_at = case when status in
                    ('completed', 'failed', 'cancelled', 'expired')
                    then updated_at else %s end
                where job_id = %s and user_id = %s
                returning wait_id, thread_id, user_id, turn_id, run_id, trace_id,
                          job_id, status, continuation_turn_id, expires_at,
                          created_at, updated_at
                """,
                (now, job_id, user_id),
            ).fetchone()
        if row is None:
            raise AgentResourceNotFound(job_id)
        return _job_wait_from_row(row)

    def save_job_completion_event(self, event: AgentJobCompletionEvent) -> None:
        Jsonb = _jsonb_type()
        with self._connect() as conn:
            conn.execute(
                """
                insert into agent_job_events (
                    event_id, event_type, job_id, thread_id, user_id, turn_id,
                    run_id, trace_id, status, error_code, attempt, occurred_at,
                    published_at, delivery_attempts, last_error
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (event_id) do nothing
                """,
                (
                    event.event_id, event.event_type, event.job_id, event.thread_id,
                    event.user_id, event.turn_id, event.run_id, event.trace_id,
                    event.status.value, event.error_code, event.attempt,
                    event.occurred_at, event.published_at, event.delivery_attempts,
                    event.last_error,
                ),
            )

    def list_pending_job_events(self, *, limit: int = 20) -> list[AgentJobCompletionEvent]:
        bounded = max(1, min(limit, 100))
        with self._connect() as conn:
            rows = conn.execute(
                """
                select event_id, event_type, job_id, thread_id, user_id, turn_id,
                       run_id, trace_id, status, error_code, attempt, occurred_at,
                       published_at, delivery_attempts, last_error
                from agent_job_events
                where published_at is null
                order by occurred_at, event_id
                limit %s
                """,
                (bounded,),
            ).fetchall()
        return [_job_event_from_row(row) for row in rows]

    def prepare_job_continuation(
        self, event: AgentJobCompletionEvent, *, now: datetime,
    ) -> AgentTurnRecord | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    select e.event_id, e.event_type, e.job_id, e.thread_id, e.user_id,
                           e.turn_id, e.run_id, e.trace_id, e.status, e.error_code,
                           e.attempt, e.occurred_at, e.published_at,
                           e.delivery_attempts, e.last_error,
                           w.status, w.continuation_turn_id,
                           j.owner_id, j.status
                    from agent_job_events e
                    join agent_job_waits w
                      on w.job_id = e.job_id
                     and w.thread_id = e.thread_id
                     and w.user_id = e.user_id
                    join jobs j
                      on j.id = e.job_id
                    where e.event_id = %s
                    for update of e, w
                    """,
                    (event.event_id,),
                ).fetchone()
                if row is None:
                    raise AgentResourceNotFound(event.event_id)
                stored = _job_event_from_row(row[:15])
                if stored.model_dump(mode="json") != event.model_dump(mode="json"):
                    raise ValueError("job completion event payload changed")
                wait_status = AgentJobWaitStatus(row[15])
                continuation_id = row[16]
                if row[17] != event.user_id or row[18] != event.status.value:
                    return None
                if wait_status in {
                    AgentJobWaitStatus.COMPLETED,
                    AgentJobWaitStatus.FAILED,
                    AgentJobWaitStatus.CANCELLED,
                    AgentJobWaitStatus.EXPIRED,
                }:
                    return None
                if wait_status is AgentJobWaitStatus.RESUME_QUEUED:
                    if not continuation_id:
                        raise ValueError("job wait is queued without a continuation turn")
                    turn_row = conn.execute(
                        """
                        select turn_id, thread_id, run_id, user_id, trace_id,
                               idempotency_key, request_hash, status, attempt,
                               error_code, created_at, updated_at, started_at, completed_at
                        from agent_turns where turn_id = %s and user_id = %s
                        """,
                        (continuation_id, event.user_id),
                    ).fetchone()
                    if turn_row is None:
                        raise AgentResourceNotFound(continuation_id)
                    return _turn_from_row(turn_row)
                turn = _continuation_turn(event, now)
                turn_row = conn.execute(
                    """
                    insert into agent_turns (
                        turn_id, thread_id, run_id, user_id, trace_id,
                        idempotency_key, request_hash, status, attempt, error_code,
                        created_at, updated_at, started_at, completed_at
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (user_id, idempotency_key) do nothing
                    returning turn_id, thread_id, run_id, user_id, trace_id,
                              idempotency_key, request_hash, status, attempt,
                              error_code, created_at, updated_at, started_at, completed_at
                    """,
                    _turn_values(turn),
                ).fetchone()
                if turn_row is None:
                    turn_row = conn.execute(
                        """
                        select turn_id, thread_id, run_id, user_id, trace_id,
                               idempotency_key, request_hash, status, attempt,
                               error_code, created_at, updated_at, started_at, completed_at
                        from agent_turns
                        where user_id = %s and idempotency_key = %s
                        """,
                        (turn.user_id, turn.idempotency_key),
                    ).fetchone()
                if turn_row is None:
                    raise RuntimeError("continuation turn insert returned no record")
                queued = _turn_from_row(turn_row)
                if (
                    queued.thread_id != turn.thread_id
                    or queued.run_id != turn.run_id
                    or queued.request_hash != turn.request_hash
                ):
                    raise IdempotencyConflict(turn.idempotency_key)
                updated = conn.execute(
                    """
                    update agent_job_waits
                    set status = 'resume_queued', continuation_turn_id = %s,
                        updated_at = %s
                    where wait_id = %s and status = 'waiting'
                    returning wait_id
                    """,
                    (queued.turn_id, now, row[0]),
                ).fetchone()
                if updated is None:
                    raise TurnConflict(event.job_id)
                return queued
        except Exception as exc:
            if _constraint_name(exc) == "agent_turns_one_active_per_thread_idx":
                raise ActiveTurnConflict(event.thread_id) from exc
            raise

    def mark_job_event_published(self, *, event_id: str, now: datetime) -> None:
        with self._connect() as conn:
            row = conn.execute(
                """
                update agent_job_events
                set published_at = coalesce(published_at, %s),
                    delivery_attempts = delivery_attempts + 1,
                    last_error = null
                where event_id = %s
                returning event_id
                """,
                (now, event_id),
            ).fetchone()
        if row is None:
            raise AgentResourceNotFound(event_id)

    def mark_job_event_failed(self, *, event_id: str, error: str, now: datetime) -> None:
        with self._connect() as conn:
            row = conn.execute(
                """
                update agent_job_events
                set delivery_attempts = delivery_attempts + 1,
                    last_error = %s
                where event_id = %s and published_at is null
                returning event_id
                """,
                (error[:500], event_id),
            ).fetchone()
        if row is None:
            raise AgentResourceNotFound(event_id)

    def complete_job_wait(
        self, *, job_id: str, user_id: str, continuation_turn_id: str,
        status: AgentJobWaitStatus, now: datetime,
    ) -> AgentJobWaitRecord:
        if status not in {
            AgentJobWaitStatus.COMPLETED,
            AgentJobWaitStatus.FAILED,
            AgentJobWaitStatus.CANCELLED,
            AgentJobWaitStatus.EXPIRED,
        }:
            raise ValueError("job wait completion requires a terminal status")
        with self._connect() as conn:
            row = conn.execute(
                """
                update agent_job_waits
                set status = case when status in
                    ('completed', 'failed', 'cancelled', 'expired')
                    then status else %s end,
                    continuation_turn_id = coalesce(continuation_turn_id, %s),
                    updated_at = %s
                where job_id = %s and user_id = %s
                  and (continuation_turn_id = %s or continuation_turn_id is null)
                returning wait_id, thread_id, user_id, turn_id, run_id, trace_id,
                          job_id, status, continuation_turn_id, expires_at,
                          created_at, updated_at
                """,
                (
                    status.value, continuation_turn_id, now, job_id, user_id,
                    continuation_turn_id,
                ),
            ).fetchone()
        if row is None:
            raise AgentResourceNotFound(job_id)
        return _job_wait_from_row(row)

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError("Install psycopg[binary]>=3.1.18 to use PostgreSQL storage") from exc
        return psycopg.connect(self.database_url)


def _thread_from_row(row) -> AgentThreadRecord:
    fields = ("thread_id", "user_id", "title", "current_run_id", "status", "version", "created_at", "updated_at")
    return AgentThreadRecord.model_validate(dict(zip(fields, row)))


def _message_from_row(row) -> AgentMessageRecord:
    fields = ("message_id", "thread_id", "run_id", "trace_id", "user_id", "role", "blocks", "created_at")
    return AgentMessageRecord.model_validate(dict(zip(fields, row)))


def _feedback_from_row(row) -> AgentFeedbackRecord:
    fields = (
        "feedback_id", "thread_id", "turn_id", "message_id", "trace_id",
        "user_id", "rating", "failure_category", "correction_text",
        "created_at", "updated_at",
    )
    return AgentFeedbackRecord.model_validate(dict(zip(fields, row)))


def _eval_candidate_from_row(row) -> AgentEvalCandidateRecord:
    fields = (
        "candidate_id", "feedback_id", "thread_id", "turn_id", "message_id",
        "trace_id", "user_id", "status", "rating", "failure_category",
        "user_message_summary", "assistant_message_summary", "correction_summary",
        "trace_summary", "created_at", "updated_at",
    )
    return AgentEvalCandidateRecord.model_validate(dict(zip(fields, row)))


def _input_file_from_row(row) -> AgentInputFileRecord:
    fields = (
        "file_id", "bundle_id", "user_id", "field", "filename", "storage_key",
        "checksum", "content_type", "size_bytes", "created_at",
    )
    return AgentInputFileRecord.model_validate(dict(zip(fields, row)))


def _job_wait_from_row(row) -> AgentJobWaitRecord:
    fields = (
        "wait_id", "thread_id", "user_id", "turn_id", "run_id", "trace_id",
        "job_id", "status", "continuation_turn_id", "expires_at", "created_at",
        "updated_at",
    )
    return AgentJobWaitRecord.model_validate(dict(zip(fields, row)))


def _job_event_from_row(row) -> AgentJobCompletionEvent:
    fields = (
        "event_id", "event_type", "job_id", "thread_id", "user_id", "turn_id",
        "run_id", "trace_id", "status", "error_code", "attempt", "occurred_at",
        "published_at", "delivery_attempts", "last_error",
    )
    return AgentJobCompletionEvent.model_validate(dict(zip(fields, row)))


def _continuation_turn(
    event: AgentJobCompletionEvent,
    now: datetime,
) -> AgentTurnRecord:
    turn_id = continuation_turn_id(event.event_id)
    idempotency_key = continuation_idempotency_key(event.event_id)
    request_hash = "sha256:" + sha256(event.event_id.encode("utf-8")).hexdigest()
    return AgentTurnRecord(
        turn_id=turn_id,
        thread_id=event.thread_id,
        run_id=event.run_id,
        user_id=event.user_id,
        trace_id=event.trace_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        status=AgentTurnStatus.QUEUED,
        attempt=0,
        error_code=None,
        created_at=now,
        updated_at=now,
        started_at=None,
        completed_at=None,
    )


def _turn_values(turn: AgentTurnRecord) -> tuple[Any, ...]:
    return (
        turn.turn_id,
        turn.thread_id,
        turn.run_id,
        turn.user_id,
        turn.trace_id,
        turn.idempotency_key,
        turn.request_hash,
        turn.status.value,
        turn.attempt,
        turn.error_code,
        turn.created_at,
        turn.updated_at,
        turn.started_at,
        turn.completed_at,
    )


def _turn_from_row(row) -> AgentTurnRecord:
    fields = (
        "turn_id", "thread_id", "run_id", "user_id", "trace_id", "idempotency_key", "request_hash",
        "status", "attempt", "error_code",
        "created_at", "updated_at", "started_at", "completed_at",
    )
    return AgentTurnRecord.model_validate(dict(zip(fields, row)))


def _trace_event_from_row(row) -> AgentTraceEvent:
    fields = (
        "event_id", "trace_id", "thread_id", "turn_id", "run_id", "user_id",
        "event_type", "component", "name", "schema_version", "graph_version",
        "prompt_version", "prompt_hash", "model_provider", "model_name",
        "tool_name", "tool_schema_hash", "job_id", "outcome", "latency_ms",
        "prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens",
        "usage_status", "retry_count", "error_code", "created_at",
    )
    return AgentTraceEvent.model_validate(dict(zip(fields, row)))


def _jsonb_type():
    try:
        from psycopg.types.json import Jsonb
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("Install psycopg[binary]>=3.1.18 to use PostgreSQL storage") from exc
    return Jsonb


def _constraint_name(exc: Exception) -> str | None:
    diagnostic = getattr(exc, "diag", None)
    return getattr(diagnostic, "constraint_name", None)
