from __future__ import annotations

import json
import logging
import os
from time import perf_counter
from typing import TYPE_CHECKING

import httpx
from pydantic import ValidationError

from .trace import ModelUsage, TraceRecorder

if TYPE_CHECKING:
    from .context import MainModelContext
    from .graph import MainModelOutput


class ModelBoundaryError(ValueError):
    """The graph model input or output violated its typed boundary."""


LOG = logging.getLogger("omicsprism.platform.agent_model")


class VllmGraphModel:
    """OpenAI-compatible structured boundary for the v3 Main graph node."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_seconds: float = 60.0,
        client: httpx.Client | None = None,
        trace_recorder: TraceRecorder | None = None,
    ) -> None:
        if not base_url.strip() or not model.strip():
            raise ValueError("vLLM base_url and model are required")
        self.model_name = model.strip()
        self.endpoint = _chat_completions_url(base_url)
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=timeout_seconds)
        self.trace_recorder = trace_recorder
        self.last_usage = ModelUsage()
        # Keep the same bounded context observation available to live eval as
        # the recorded fixture model. Contexts contain no raw dataset rows.
        self.contexts: list[MainModelContext] = []
        self._debug_raw_output = os.getenv("OMICS_PRISM_AGENT_DEBUG_RAW_OUTPUT", "").lower() in {
            "1", "true", "yes", "on"
        }

    def __call__(self, context: MainModelContext) -> MainModelOutput:
        from .context import MainModelContext
        from .graph import MainModelOutput

        if not isinstance(context, MainModelContext):
            raise ModelBoundaryError("graph model context has an invalid type")
        self.contexts.append(context)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        started = perf_counter()
        self.last_usage = ModelUsage()
        try:
            raw_content: str | None = None
            response = self.client.post(
                self.endpoint,
                headers=headers,
                json={
                "model": self.model_name,
                "temperature": 0,
                "max_tokens": 768,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "main_model_output",
                        "strict": True,
                        "schema": MainModelOutput.model_json_schema(),
                    },
                },
                "chat_template_kwargs": {"enable_thinking": False},
                "messages": [
                    {"role": "system", "content": _GRAPH_MAIN_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(
                            context.model_dump(mode="json"), ensure_ascii=False
                        ),
                    },
                ],
                },
            )
            self.last_usage = _usage_from_response(response)
            response.raise_for_status()
            try:
                content = response.json()["choices"][0]["message"]["content"]
                raw_content = content if isinstance(content, str) else None
                payload = json.loads(content)
                decision = payload.get("decision") if isinstance(payload, dict) else None
                action = decision.get("action") if isinstance(decision, dict) else None
                answer_present = isinstance(payload, dict) and "answer" in payload
                answer = payload.get("answer") if answer_present else None
                LOG.info(
                    "vLLM model output: action=%r answer_present=%s answer_is_null=%s answer_length=%s",
                    action,
                    answer_present,
                    answer is None,
                    len(answer) if isinstance(answer, str) else 0,
                )
                if self._debug_raw_output:
                    # Opt-in diagnostics only. Keep this bounded and out of
                    # trace/report persistence because it may contain user text.
                    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                    LOG.warning(
                        "vLLM raw model output (debug only): %s",
                        raw[:4000],
                        extra={"event": "agent.model.raw_output_debug"},
                    )
                _drop_irrelevant_action_fields(payload)
                result = MainModelOutput.model_validate(payload)
            except (KeyError, IndexError, TypeError, ValueError, ValidationError) as exc:
                if self._debug_raw_output:
                    LOG.warning(
                        "vLLM response validation details (debug only): error_type=%s error=%s raw_content=%s",
                        type(exc).__name__,
                        str(exc)[:1000],
                        (raw_content or "")[:4000],
                        extra={"event": "agent.model.validation_debug"},
                    )
                LOG.warning(
                    "vLLM graph response rejected",
                    extra={"event": "agent.model.response_rejected", "error_code": type(exc).__name__},
                )
                raise ModelBoundaryError("vLLM graph response is invalid") from exc
            else:
                self._record_call(context, started, outcome="accepted")
                return result
        except httpx.HTTPStatusError as exc:
            self._record_call(context, started, outcome="http_error", error_code="HTTPStatusError")
            raise
        except Exception as exc:
            self._record_call(context, started, outcome="rejected", error_code=type(exc).__name__)
            raise

    def _record_call(
        self,
        context: MainModelContext,
        started: float,
        *,
        outcome: str,
        error_code: str | None = None,
    ) -> None:
        if self.trace_recorder is None:
            return
        self.trace_recorder.model_call(
            context=context,
            model_name=self.model_name,
            system_prompt=_GRAPH_MAIN_SYSTEM_PROMPT,
            schema_version="main-model-output.v1",
            usage=self.last_usage,
            latency_ms=round((perf_counter() - started) * 1000, 3),
            retry_count=0,
            outcome=outcome,
            error_code=error_code,
        )


def _drop_irrelevant_action_fields(payload: object) -> None:
    """Drop semantically incompatible optional fields before typed validation.

    The flat output schema is intentionally shared with vLLM, but the business
    contract still has action-specific fields. Some models fill optional fields
    from another branch even when strict JSON generation is enabled.
    """

    if not isinstance(payload, dict):
        return
    decision = payload.get("decision")
    if not isinstance(decision, dict):
        return
    action = decision.get("action")
    if action != "tool_call":
        decision["tool"] = None
        decision["arguments"] = {}
    if action != "query_result":
        decision["result_query"] = None
    if action != "grounded_answer":
        decision["grounded_answer"] = None
    if action not in {"get_job", "query_result"}:
        decision["job_id"] = None
    if action != "ask_user":
        decision["question"] = None
    if action not in {"inspect_dataset", "run_analysis", "propose_plan"}:
        decision["analysis_type"] = None
        decision["proposal"] = None
    if action != "answer":
        payload["answer"] = None


def _chat_completions_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    if normalized.endswith("/v1"):
        return normalized + "/chat/completions"
    return normalized + "/v1/chat/completions"


_GRAPH_MAIN_SYSTEM_PROMPT = (
    "You are OmicsPrism Copilot. Return exactly one object matching the supplied "
    "MainModelOutput schema. A tool_call may invoke only the read-only tools "
    "describe_metadata, enumerate_contrasts, list_jobs, describe_artifacts, or "
    "query_artifact; provide typed arguments and wait for its observation before "
    "deciding. Use answer, grounded_answer, or ask_user as terminal LoopExit actions. "
    "Route general knowledge to answer; dataset inspection or DEG/DEM/GMA requests "
    "to inspect_dataset, run_analysis, or propose_plan; existing Job status "
    "or evidence questions to get_job or query_result. AnalysisProposal values are "
    "candidates only and must use observed dataset roles and explicit user language. "
    "A grounded_answer must cite the artifact, checksum, and row IDs from the latest "
    "successful query observation; never invent citations or numeric values. Never "
    "When action is answer, answer is required and must be a concise non-empty response. "
    "When action is ask_user, question is required. For every other action, answer must be null. "
    "claim a dataset fact, Job, artifact, entity, or numeric result that is absent "
    "from the bounded context. Do not decide validation, ownership, ambiguity, or "
    "execution success."
)


def _usage_from_response(response: httpx.Response) -> ModelUsage:
    try:
        usage = response.json().get("usage")
    except (TypeError, ValueError):
        usage = None
    if not isinstance(usage, dict):
        return ModelUsage()
    prompt = _nonnegative_int(usage.get("prompt_tokens"))
    completion = _nonnegative_int(usage.get("completion_tokens"))
    total = _nonnegative_int(usage.get("total_tokens"))
    cached = _nonnegative_int(
        usage.get("cached_tokens", usage.get("prompt_tokens_details", {}).get("cached_tokens"))
        if isinstance(usage.get("prompt_tokens_details", {}), dict)
        else usage.get("cached_tokens")
    )
    return ModelUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cached_tokens=cached,
        status="reported" if any(item is not None for item in (prompt, completion, total, cached)) else "unknown",
    )


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return result if result is not None and result >= 0 else None
