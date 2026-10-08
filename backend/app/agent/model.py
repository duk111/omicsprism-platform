from __future__ import annotations

import json
import logging
import os
from time import perf_counter

import httpx
from pydantic import ValidationError

from .graph import (
    AgentRole,
    AnalysisModelOutput,
    QaModelOutput,
    RouteClassification,
    ResultQaModelOutput,
)
from .trace import ModelUsage, TraceRecorder
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
        structured_tool_response: bool = True,
    ) -> None:
        if not base_url.strip() or not model.strip():
            raise ValueError("vLLM base_url and model are required")
        self.model_name = model.strip()
        self.endpoint = _chat_completions_url(base_url)
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=timeout_seconds)
        self.trace_recorder = trace_recorder
        self.structured_tool_response = structured_tool_response
        self._structured_tools_supported: bool | None = None
        self.last_usage = ModelUsage()
        self.last_tool_calls: list[dict[str, object]] = []
        self.last_assistant_message: dict[str, object] | None = None
        # Keep the same bounded context observation available to live eval as
        # the recorded fixture model. Contexts contain no raw dataset rows.
        self.contexts: list[object] = []
        self.last_request_payload: dict[str, object] | None = None
        self.last_role: str | None = None
        # A graph turn may invoke the model several times while executing
        # read-only tools. Keep the OpenAI-compatible chat transcript in
        # memory so later calls append only new assistant/tool messages to the
        # stable seed history (vLLM can then reuse its prefix cache).
        # The graph state remains the source of truth and is still recorded in
        # ``contexts`` for tracing and tests.
        self._chat_histories: dict[tuple[str, str, str, str], list[dict[str, object]]] = {}
        self._chat_observation_counts: dict[tuple[str, str, str, str], int] = {}
        self._chat_fingerprints: dict[tuple[str, str, str, str], str] = {}
        self._chat_last_guidance: dict[tuple[str, str, str, str], str | None] = {}
        self._chat_tool_call_ids: dict[tuple[str, str, str, str], list[str]] = {}
        self._chat_last_summary: dict[tuple[str, str, str, str], str | None] = {}
        self._debug_raw_output = os.getenv("OMICS_PRISM_AGENT_DEBUG_RAW_OUTPUT", "").lower() in {
            "1", "true", "yes", "on"
        }

    def __call__(
        self,
        context: object,
        *,
        role: AgentRole,
    ) -> QaModelOutput | AnalysisModelOutput | ResultQaModelOutput | RouteClassification:
        from .context import (
            AnalysisModelContext,
            QaModelContext,
            RouterModelContext,
            ResultQaModelContext,
        )
        from .graph import AgentRole

        if not isinstance(role, AgentRole):
            try:
                role = AgentRole(role)
            except ValueError as exc:
                raise ModelBoundaryError("agent role is invalid") from exc
        role_contexts = {
            AgentRole.QA: QaModelContext,
            AgentRole.ANALYSIS: AnalysisModelContext,
            AgentRole.RESULT_QA: ResultQaModelContext,
            AgentRole.ROUTER: RouterModelContext,
        }
        context_type = role_contexts[role]
        if not isinstance(context, context_type):
            raise ModelBoundaryError(
                f"{role.value} model context has an invalid type"
            )
        role_config = _ROLE_CONFIG[role]
        output_model = role_config["output_model"]
        system_prompt = role_config["system_prompt"]
        schema_name = role_config["schema_name"]
        tool_names = role_config["tool_names"]
        self.contexts.append(context)
        self.last_role = role.value
        context_thread_id = str(getattr(context, "thread_id", "thread-local"))
        context_turn_id = str(getattr(context, "turn_id", "turn-local"))
        context_run_id = str(getattr(context, "run_id", "run-local"))
        chat_key = (
            role.value,
            context_thread_id,
            context_turn_id,
            context_run_id,
        )
        fingerprint = _context_fingerprint(context, role=role)
        history = self._chat_histories.get(chat_key)
        if history is None or self._chat_fingerprints.get(chat_key) != fingerprint:
            if history is None and len(self._chat_histories) >= 64:
                oldest_key = next(iter(self._chat_histories))
                self._chat_histories.pop(oldest_key, None)
                self._chat_observation_counts.pop(oldest_key, None)
                self._chat_fingerprints.pop(oldest_key, None)
                self._chat_last_guidance.pop(oldest_key, None)
                self._chat_tool_call_ids.pop(oldest_key, None)
                self._chat_last_summary.pop(oldest_key, None)
            history = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        context.model_dump(mode="json"), ensure_ascii=False
                    ),
                },
            ]
            self._chat_histories[chat_key] = history
            self._chat_observation_counts[chat_key] = 0
            self._chat_fingerprints[chat_key] = fingerprint
            self._chat_last_guidance[chat_key] = getattr(context, "tool_repetition_guidance", None)
            self._chat_last_summary[chat_key] = getattr(context, "conversation_summary", None)
            self._chat_tool_call_ids[chat_key] = []
            for observation in getattr(context, "tool_observations", []) or []:
                observation_index = self._chat_observation_counts[chat_key]
                call_id = observation.call_id or self._tool_call_id(chat_key, observation_index)
                self._remember_tool_call_id(chat_key, observation_index, call_id)
                history.append(observation.assistant_message or {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": _enum_value(observation.tool),
                            "arguments": json.dumps(observation.arguments, ensure_ascii=False),
                        },
                    }],
                })
                history.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": observation.summary,
                })
                self._chat_observation_counts[chat_key] += 1
        else:
            observed_count = self._chat_observation_counts.get(chat_key, 0)
            for observation in (getattr(context, "tool_observations", []) or [])[observed_count:]:
                call_id = observation.call_id or self._tool_call_id(chat_key, observed_count)
                self._remember_tool_call_id(chat_key, observed_count, call_id)
                last_assistant = next(
                    (item for item in reversed(history) if item.get("role") == "assistant"),
                    None,
                )
                last_calls = last_assistant.get("tool_calls", []) if last_assistant else []
                if not last_calls or last_calls[-1].get("id") != call_id:
                    # A graph guard may have forced a tool call after the
                    # model returned a terminal action. Make the resulting
                    # tool message a valid pair in the replayed transcript.
                    history.append(observation.assistant_message or {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": _enum_value(observation.tool),
                                "arguments": json.dumps(observation.arguments, ensure_ascii=False),
                            },
                        }],
                    })
                history.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": observation.summary,
                })
                observed_count += 1
            observations = getattr(context, "tool_observations", []) or []
            self._chat_observation_counts[chat_key] = len(observations)
            guidance = getattr(context, "tool_repetition_guidance", None)
            previous_guidance = self._chat_last_guidance.get(chat_key)
            if guidance and guidance != previous_guidance:
                # The deterministic repetition guard is a new observation
                # from the graph. Complete the just-returned assistant tool
                # call with a tool-role policy result instead of inserting a
                # user message between an assistant call and its result.
                last_assistant = next(
                    (item for item in reversed(history) if item.get("role") == "assistant"),
                    None,
                )
                last_calls = last_assistant.get("tool_calls", []) if last_assistant else []
                call_id = last_calls[-1].get("id") if last_calls else None
                if isinstance(call_id, str) and call_id:
                    history.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": guidance,
                    })
                else:
                    history.append({"role": "user", "content": guidance})
            self._chat_last_guidance[chat_key] = guidance
            previous_summary = self._chat_last_summary.get(chat_key)
            conversation_summary = getattr(context, "conversation_summary", None)
            if conversation_summary != previous_summary and conversation_summary:
                delta = conversation_summary
                if previous_summary and delta.startswith(previous_summary):
                    delta = delta[len(previous_summary):].lstrip()
                if delta:
                    history.append({"role": "user", "content": delta})
            self._chat_last_summary[chat_key] = conversation_summary
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        started = perf_counter()
        self.last_usage = ModelUsage()
        self.last_tool_calls = []
        self.last_assistant_message = None
        from .capabilities import readonly_openai_tool_definitions
        try:
            raw_content: str | None = None
            request_payload: dict[str, object] = {
                "model": self.model_name,
                "temperature": 0,
                "max_tokens": 768,
                "chat_template_kwargs": {"enable_thinking": False},
                "tools": readonly_openai_tool_definitions(names=tool_names),
                "parallel_tool_calls": False,
                "messages": history,
            }
            if tool_names is not None:
                request_payload["tool_choice"] = "auto" if tool_names else "none"
            else:
                request_payload["tool_choice"] = "auto"
            if self.structured_tool_response and self._structured_tools_supported is not False:
                request_payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "strict": True,
                        "schema": output_model.model_json_schema(),
                    },
                }
            self.last_request_payload = request_payload
            response = self.client.post(self.endpoint, headers=headers, json=request_payload)
            if (
                response.status_code == 400
                and self.structured_tool_response
                and self._structured_tools_supported is not False
                and _response_mentions_unsupported_response_format(response)
            ):
                # Some vLLM releases support native tools and JSON schema
                # responses independently, but reject them in one request.
                # Retry the same turn in native tool mode without losing the
                # transcript or charging a second graph step.
                fallback_payload = dict(request_payload)
                fallback_payload.pop("response_format", None)
                self._structured_tools_supported = False
                response = self.client.post(
                    self.endpoint,
                    headers=headers,
                    json=fallback_payload,
                )
            self.last_usage = _usage_from_response(response)
            response.raise_for_status()
            try:
                message = response.json()["choices"][0]["message"]
                if not isinstance(message, dict):
                    raise TypeError("model message is not an object")
                self.last_tool_calls = [
                    item for item in (message.get("tool_calls") or [])
                    if isinstance(item, dict)
                ]
                if len(self.last_tool_calls) > 1:
                    raise ValueError("parallel tool calls are not supported")
                self.last_assistant_message = _standard_assistant_message(message)
                content = message.get("content")
                raw_content = content if isinstance(content, str) else None
                native_tool_call = bool(self.last_tool_calls)
                if native_tool_call:
                    call = self.last_tool_calls[0]
                    function = call.get("function") if isinstance(call, dict) else None
                    if not isinstance(function, dict):
                        raise ValueError("tool call function is invalid")
                    tool_name = function.get("name")
                    raw_arguments = function.get("arguments", "{}")
                    if not isinstance(tool_name, str) or not isinstance(raw_arguments, str):
                        raise ValueError("tool call fields are invalid")
                    arguments = json.loads(raw_arguments)
                    payload = {
                        "decision": {
                            "action": "tool_call",
                            "tool": tool_name,
                            "arguments": arguments,
                        },
                        "answer": None,
                    }
                else:
                    if not isinstance(content, str) or not content.strip():
                        raise ValueError("model response has no content")
                    try:
                        payload = json.loads(content)
                    except json.JSONDecodeError:
                        # Native tool mode permits ordinary assistant text for
                        # terminal turns; retain the graph's typed boundary.
                        payload = {
                            "decision": {"action": "answer"},
                            "answer": content.strip(),
                        }
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
                result = output_model.model_validate(payload)
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
                # Preserve exactly what the model decided so the next call in
                # this turn can replay it as an assistant message.
                history.append(
                    self.last_assistant_message
                    or {"role": "assistant", "content": raw_content or json.dumps(payload, ensure_ascii=False)}
                )
                for index, call in enumerate(self.last_tool_calls):
                    call_id = call.get("id") if isinstance(call, dict) else None
                    if isinstance(call_id, str) and call_id.strip():
                        self._remember_tool_call_id(chat_key, index, call_id.strip())
                self._record_call(context, started, outcome="accepted")
                return result
        except httpx.HTTPStatusError as exc:
            self._record_call(context, started, outcome="http_error", error_code="HTTPStatusError")
            raise
        except Exception as exc:
            self._record_call(context, started, outcome="rejected", error_code=type(exc).__name__)
            raise

    def resolve_clarification(self, context: object) -> object:
        """Run the bounded clarification sub-agent without tools or graph state."""

        from .clarification_resolver import ClarificationResolverInput, ClarificationResolverOutput

        request = ClarificationResolverInput.model_validate(context)
        system_prompt = (
            "You are the OmicsPrism clarification resolver. Classify only the user's "
            "reply to the active analysis clarification as edit_params, confirm, cancel, "
            "param_question, or unrelated. Map edits only to the supplied option IDs or "
            "parameter names. Never invent a dataset fact, option, or parameter. Return "
            "the strict ClarificationResolverOutput schema and use the user's language."
        )
        payload: dict[str, object] = {
            "model": self.model_name,
            "temperature": 0,
            "max_tokens": 512,
            "chat_template_kwargs": {"enable_thinking": False},
            "tool_choice": "none",
            "tools": [],
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(request.model_dump(mode="json"), ensure_ascii=False),
                },
            ],
        }
        if self.structured_tool_response and self._structured_tools_supported is not False:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "clarification_resolver_output",
                    "strict": True,
                    "schema": ClarificationResolverOutput.model_json_schema(),
                },
            }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        self.last_request_payload = payload
        response = self.client.post(self.endpoint, headers=headers, json=payload)
        if (
            response.status_code == 400
            and "response_format" in payload
            and _response_mentions_unsupported_response_format(response)
        ):
            fallback = dict(payload)
            fallback.pop("response_format", None)
            self._structured_tools_supported = False
            response = self.client.post(self.endpoint, headers=headers, json=fallback)
        self.last_usage = _usage_from_response(response)
        response.raise_for_status()
        try:
            content = response.json()["choices"][0]["message"]["content"]
            result = json.loads(content) if isinstance(content, str) else content
            return ClarificationResolverOutput.model_validate(result)
        except (KeyError, IndexError, TypeError, ValueError, ValidationError) as exc:
            raise ModelBoundaryError("clarification resolver response is invalid") from exc

    def _record_call(
        self,
        context: object,
        started: float,
        *,
        outcome: str,
        error_code: str | None = None,
    ) -> None:
        if self.trace_recorder is None:
            return
        if not all(hasattr(context, name) for name in ("trace_id", "thread_id", "turn_id", "user_id")):
            LOG.info(
                "skipping model trace for role context without correlation identifiers",
                extra={"event": "agent.model.trace_skipped", "agent_role": self.last_role},
            )
            return
        from .graph import AgentRole
        role_config = _ROLE_CONFIG[AgentRole(self.last_role)]
        self.trace_recorder.model_call(
            context=context,
            model_name=self.model_name,
            system_prompt=role_config["system_prompt"],
            schema_version=role_config["schema_version"],
            usage=self.last_usage,
            latency_ms=round((perf_counter() - started) * 1000, 3),
            retry_count=0,
            outcome=outcome,
            failure_code="model_unavailable" if outcome in {"http_error", "rejected"} else None,
        )

    def _tool_call_id(self, chat_key: tuple[str, str, str, str], index: int) -> str:
        ids = self._chat_tool_call_ids.setdefault(chat_key, [])
        while len(ids) <= index:
            ids.append(f"call-{len(ids) + 1}")
        return ids[index]

    def _remember_tool_call_id(self, chat_key: tuple[str, str, str, str], index: int, call_id: str) -> None:
        ids = self._chat_tool_call_ids.setdefault(chat_key, [])
        while len(ids) <= index:
            ids.append(f"call-{len(ids) + 1}")
        ids[index] = call_id


def _chat_completions_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    if normalized.endswith("/v1"):
        return normalized + "/chat/completions"
    return normalized + "/v1/chat/completions"


def _context_fingerprint(context: object, *, role: AgentRole | None = None) -> str:
    """Identify the seed prompt for a graph turn without hashing tool output."""

    model_dump = getattr(context, "model_dump", None)
    if callable(model_dump):
        serialized = model_dump(mode="json", exclude={"tool_observations"})
    else:
        serialized = str(context)
    payload = {
        "role": role.value if role is not None else "main",
        "thread_id": getattr(context, "thread_id", "thread-local"),
        "turn_id": getattr(context, "turn_id", "turn-local"),
        "run_id": getattr(context, "run_id", "run-local"),
        "context": serialized,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _enum_value(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw)


def _standard_assistant_message(message: dict[str, object]) -> dict[str, object]:
    """Keep only OpenAI-compatible fields for transcript replay."""

    result: dict[str, object] = {
        "role": "assistant",
        "content": message.get("content"),
    }
    if isinstance(result["content"], str):
        result["content"] = result["content"][:4000]
    if isinstance(message.get("tool_calls"), list):
        calls: list[dict[str, object]] = []
        for call in message["tool_calls"]:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if not isinstance(function, dict):
                continue
            calls.append({
                "id": str(call.get("id", ""))[:200],
                "type": "function",
                "function": {
                    "name": str(function.get("name", ""))[:100],
                    "arguments": str(function.get("arguments", "{}"))[:4000],
                },
            })
        result["tool_calls"] = calls
    if message.get("reasoning_content") is not None:
        # vLLM/Qwen may require this vendor extension when replaying a
        # tool-call assistant turn, even though it is not part of the core
        # OpenAI message shape.
        reasoning = message["reasoning_content"]
        if reasoning:
            result["reasoning_content"] = reasoning
    if message.get("refusal") is not None:
        result["refusal"] = message["refusal"]
    return result


_QA_SYSTEM_PROMPT = (
    "You are the OmicsPrism general QA agent. Answer general questions and use only "
    "the supplied recent messages and dataset role names. Do not infer metadata fields, "
    "analysis parameters, Jobs, artifacts, or numeric results that are not present. "
    "Return one typed decision: answer for a direct response, ask_user when the user "
    "must clarify, or reroute when the request belongs to analysis or result QA. "
    "Use the same language as the user's latest message."
)

_ROUTER_SYSTEM_PROMPT = (
    "Classify only the user's requested destination: qa for general knowledge or "
    "conversation, analysis for dataset capability/planning/execution, result_qa for "
    "owned Job status or result evidence. Use only user_message, recent_messages, "
    "dataset_roles, has_jobs, and active_pending_analysis. Return ambiguous with low "
    "confidence when intent is unclear. Never choose tools, parameters, or execution."
)

_ANALYSIS_SYSTEM_PROMPT = (
    "You are the OmicsPrism analysis agent. Use the bounded fact_index metadata fields, "
    "the current user_message, recent_messages, metadata levels, dataset roles, "
    "pending_analysis, and decision_ledger to propose "
    "capability_query for capability requests, using only an optional analysis_type. "
    "For an analysis request, propose candidate parameters instead of asserting the user's intent. "
    "Candidate compare_field, tested_level, reference_level, scope, and min_replicates are hypotheses "
    "that must be validated and shown to the user for confirmation before a plan or Job is created. "
    "Use only metadata fields and levels present in the supplied bounded facts. A field that looks like "
    "a replicate, batch, time, line, treatment, condition, or group column has no fixed platform meaning; "
    "do not select it as a comparison or blocking field solely from its name. Do not invent analyses from "
    "metadata column names. Treat replicate-like columns as possible biological-replicate annotations only "
    "when the file evidence and user request support that interpretation, and ask for confirmation when ambiguous. "
    "analysis decisions. Infer compare_field and scope only from observed metadata and "
    "explicit user language. Use tool_call with describe_metadata or enumerate_contrasts "
    "when the bounded facts are insufficient. Return answer for a bounded read-only explanation, "
    "capability_query, inspect_dataset, propose_plan, or "
    "run_analysis for analysis work, ask_user only as an internal typed need-for-input action, or reroute for "
    "a general or result question. Do not claim validation or submit a Job. Use the user's language. "
    "When a tool returns ok=false, treat its error_code as a fact from the data boundary: do not repeat "
    "the same call unchanged. If the error means a user choice is missing (for example scope_unknown), "
    "return ask_user with the candidate parameters you already know so the next user turn can continue "
    "the pending analysis. "
    "For common bioinformatics inputs, counts matrices normally contain one feature identifier column "
    "and one column per sample, while metadata/group tables normally contain one row per sample plus "
    "annotation columns. These are format conventions, not guarantees about user column names. "
    "Never infer a comparison design or sample sufficiency from a column label alone; use deterministic "
    "validation facts and ask the user to confirm inferred experimental factors. "
    "When pending_analysis contains a validated candidate and candidate_validation facts, return a concise "
    "answer asking the user to confirm or correct that candidate; do not return a literal ask_user label, "
    "change validated counts, or submit a Job."
)

_RESULT_QA_SYSTEM_PROMPT = (
    "You are the OmicsPrism result QA agent. Use only the supplied ownership-bound Job "
    "references, current user_message, recent_messages, in-scope Job IDs, and artifact index. "
    "Use tool_call with list_jobs, describe_artifacts, or query_artifact when evidence is "
    "needed. Return query_result for an "
    "evidence request, get_job for Job status, grounded_answer only when evidence is "
    "available, answer for bounded Job/artifact summaries, ask_user when a Job must be selected, "
    "or reroute for analysis or general "
    "questions. Never invent artifacts, citations, or numeric values. Use the user's language."
)

_ROLE_CONFIG: dict[AgentRole, dict[str, object]] = {
    AgentRole.QA: {
        "system_prompt": _QA_SYSTEM_PROMPT,
        "output_model": QaModelOutput,
        "schema_name": "qa_model_output",
        "schema_version": "qa-model-output.v1",
        "tool_names": set(),
    },
    AgentRole.ANALYSIS: {
        "system_prompt": _ANALYSIS_SYSTEM_PROMPT,
        "output_model": AnalysisModelOutput,
        "schema_name": "analysis_model_output",
        "schema_version": "analysis-model-output.v2",
        "tool_names": {"describe_metadata", "enumerate_contrasts"},
    },
    AgentRole.RESULT_QA: {
        "system_prompt": _RESULT_QA_SYSTEM_PROMPT,
        "output_model": ResultQaModelOutput,
        "schema_name": "result_qa_model_output",
        "schema_version": "result-qa-model-output.v1",
        "tool_names": {"list_jobs", "describe_artifacts", "query_artifact"},
    },
    AgentRole.ROUTER: {
        "system_prompt": _ROUTER_SYSTEM_PROMPT,
        "output_model": RouteClassification,
        "schema_name": "route_classification",
        "schema_version": "route-classification.v1",
        "tool_names": set(),
    },
}


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


def _response_mentions_unsupported_response_format(response: httpx.Response) -> bool:
    try:
        body = response.text.casefold()
    except Exception:
        return False
    mentions_feature = any(
        marker in body for marker in ("response_format", "structured output", "json schema")
    )
    return mentions_feature and any(
        marker in body for marker in ("not support", "unsupported", "not compatible", "cannot", "invalid")
    )


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return result if result is not None and result >= 0 else None
