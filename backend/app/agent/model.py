from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import httpx
from pydantic import ValidationError

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
    ) -> None:
        if not base_url.strip() or not model.strip():
            raise ValueError("vLLM base_url and model are required")
        self.model_name = model.strip()
        self.endpoint = _chat_completions_url(base_url)
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=timeout_seconds)

    def __call__(self, context: MainModelContext) -> MainModelOutput:
        from .context import MainModelContext
        from .graph import MainModelOutput

        if not isinstance(context, MainModelContext):
            raise ModelBoundaryError("graph model context has an invalid type")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
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
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = _response_error_detail(response)
            message = f"{exc}; vllm_error={detail}" if detail else str(exc)
            raise httpx.HTTPStatusError(
                message, request=exc.request, response=exc.response
            ) from exc
        content: object = None
        try:
            content = response.json()["choices"][0]["message"]["content"]
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
            _drop_irrelevant_action_fields(payload)
            return MainModelOutput.model_validate(payload)
        except (KeyError, IndexError, TypeError, ValueError, ValidationError) as exc:
            LOG.warning(
                "vLLM graph response rejected: raw_content=%s",
                content[:4000] if isinstance(content, str) else repr(content),
                exc_info=True,
            )
            raise ModelBoundaryError("vLLM graph response is invalid") from exc


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


def _response_error_detail(
    response: httpx.Response, *, max_chars: int = 1000
) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    candidates: list[object] = []
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            candidates.extend((error.get("message"), error.get("detail")))
        candidates.extend((payload.get("message"), payload.get("detail")))
    for candidate in candidates:
        if candidate:
            return " ".join(str(candidate).split())[:max_chars]
    return ""


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
