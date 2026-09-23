from __future__ import annotations

import json
import logging
import os
from time import perf_counter
from typing import TYPE_CHECKING

import httpx
from pydantic import ValidationError

from .graph import (
    AgentRole,
    AnalysisModelOutput,
    MainModelOutput,
    QaModelOutput,
    ResultQaModelOutput,
)
from .trace import ModelUsage, TraceRecorder
from .router import (
    _is_explicit_analysis_request as _router_is_explicit_analysis_request,
    _is_explicit_job_status_request as _router_is_explicit_job_status_request,
    _is_explicit_jobs_listing as _router_is_explicit_jobs_listing,
)

if TYPE_CHECKING:
    from .context import (
        AnalysisModelContext,
        MainModelContext,
        QaModelContext,
        ResultQaModelContext,
    )
    from .graph import (
        AgentRole,
        MainModelOutput,
        QaModelOutput,
        AnalysisModelOutput,
        ResultQaModelOutput,
    )


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
        role: AgentRole | None = None,
    ) -> MainModelOutput | QaModelOutput | AnalysisModelOutput | ResultQaModelOutput:
        from .context import (
            AnalysisModelContext,
            MainModelContext,
            QaModelContext,
            ResultQaModelContext,
        )
        from .graph import (
            AgentRole,
            AnalysisModelOutput,
            MainModelOutput,
            QaModelOutput,
            ResultQaModelOutput,
        )

        if role is not None and not isinstance(role, AgentRole):
            try:
                role = AgentRole(role)
            except ValueError as exc:
                raise ModelBoundaryError("agent role is invalid") from exc
        role_contexts = {
            AgentRole.QA: QaModelContext,
            AgentRole.ANALYSIS: AnalysisModelContext,
            AgentRole.RESULT_QA: ResultQaModelContext,
        }
        if role is None:
            if not isinstance(context, MainModelContext):
                raise ModelBoundaryError("graph model context has an invalid type")
            output_model = MainModelOutput
            system_prompt = _GRAPH_MAIN_SYSTEM_PROMPT
            schema_name = "main_model_output"
            tool_names: set[str] | None = None
        else:
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
        self.last_role = role.value if role is not None else None
        context_thread_id = str(getattr(context, "thread_id", "thread-local"))
        context_turn_id = str(getattr(context, "turn_id", "turn-local"))
        context_run_id = str(getattr(context, "run_id", "run-local"))
        chat_key = (
            role.value if role is not None else "main",
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
                if role is None and not native_tool_call:
                    _normalize_legacy_action_fields(payload, context)
                    _normalize_result_query_artifact(payload, context)
                    _normalize_explicit_jobs_tool(payload, context)
                    _normalize_result_evidence_tool(payload, context)
                    _normalize_explicit_business_actions(payload, context)
                if role is None:
                    _drop_irrelevant_action_fields(payload)
                elif native_tool_call:
                    raise ValueError("role-specific native tool calls are not supported")
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
        active_role = AgentRole(self.last_role) if self.last_role is not None else None
        role_config = _ROLE_CONFIG.get(active_role) if active_role is not None else None
        self.trace_recorder.model_call(
            context=context,
            model_name=self.model_name,
            system_prompt=(role_config["system_prompt"] if role_config else _GRAPH_MAIN_SYSTEM_PROMPT),
            schema_version=(role_config["schema_version"] if role_config else "main-model-output.v1"),
            usage=self.last_usage,
            latency_ms=round((perf_counter() - started) * 1000, 3),
            retry_count=0,
            outcome=outcome,
            error_code=error_code,
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


def _normalize_legacy_action_fields(payload: object, context: MainModelContext) -> None:
    """Translate bounded legacy argument shapes into the typed action fields.

    Some instruct models follow the generic ``arguments`` object even when an
    action has a dedicated typed field. Only known scalar keys are copied, and
    the normal Pydantic/domain validation still runs afterwards.
    """

    if not isinstance(payload, dict):
        return
    decision = payload.get("decision")
    if not isinstance(decision, dict):
        return
    arguments = decision.get("arguments")
    if not isinstance(arguments, dict):
        return
    action = decision.get("action")

    if action in {"inspect_dataset", "run_analysis", "propose_plan"}:
        proposal = decision.get("proposal")
        if proposal is None:
            proposal_values: dict[str, object] = {}
            for key in (
                "analysis_type",
                "compare_field",
                "tested_level",
                "reference_level",
                "scope",
                "requested_params",
            ):
                if key in arguments:
                    proposal_values[key] = arguments[key]
            if proposal_values:
                decision["proposal"] = proposal_values

    if action in {"get_job", "query_result"} and not decision.get("job_id"):
        job_id = arguments.get("job_id")
        if isinstance(job_id, str) and job_id.strip():
            decision["job_id"] = job_id.strip()

    if action == "query_result" and decision.get("result_query") is None:
        artifact = arguments.get("artifact")
        job_id = decision.get("job_id")
        if not isinstance(artifact, str) or not artifact.strip():
            artifacts = context.fact_index.job_artifacts.get(job_id, []) if isinstance(job_id, str) else []
            if len(artifacts) == 1:
                artifact = artifacts[0]
        if isinstance(artifact, str) and artifact.strip():
            query: dict[str, object] = {"artifact": artifact.strip()}
            for source, target in (
                ("field_path", "field_path"),
                ("filters", "filters"),
                ("sort", "sort"),
                ("limit", "limit"),
                ("resolve_entity", "resolve_entity"),
            ):
                if source in arguments:
                    query[target] = arguments[source]
            raw_query = arguments.get("query")
            if isinstance(raw_query, str) and raw_query.strip() and "resolve_entity" not in query:
                query["resolve_entity"] = raw_query.strip()
            decision["result_query"] = query


def _normalize_result_query_artifact(payload: object, context: MainModelContext) -> None:
    """Correct an entity accidentally placed in ``result_query.artifact``.

    This is limited to a Job with exactly one known artifact. Unknown names
    containing an extension remain untouched so genuine artifact errors remain
    visible to the result boundary.
    """

    if not isinstance(payload, dict):
        return
    decision = payload.get("decision")
    if not isinstance(decision, dict) or decision.get("action") != "query_result":
        return
    query = decision.get("result_query")
    if not isinstance(query, dict):
        return
    job_id = decision.get("job_id")
    artifact_map = context.fact_index.job_artifacts
    if not isinstance(job_id, str) and len(artifact_map) == 1:
        job_id = next(iter(artifact_map))
        decision["job_id"] = job_id
    artifacts = artifact_map.get(job_id, []) if isinstance(job_id, str) else []
    if len(artifacts) != 1:
        return
    artifact = query.get("artifact")
    if not isinstance(artifact, str) or not artifact.strip() or artifact in artifacts:
        return
    if query.get("resolve_entity") is not None or "." not in artifact:
        query["resolve_entity"] = query.get("resolve_entity") or artifact.strip()
        query["artifact"] = artifacts[0]


def _normalize_explicit_jobs_tool(payload: object, context: MainModelContext) -> None:
    """Repair a malformed tool branch for an explicit jobs-list request.

    Some instruct models emit ``action=tool_call`` with a null or invented
    tool name despite the constrained schema. For this one unambiguous,
    read-only intent, selecting ``list_jobs`` is deterministic and preserves
    the capability boundary. Other malformed tool calls remain rejected.
    """

    if not isinstance(payload, dict):
        return
    decision = payload.get("decision")
    if not isinstance(decision, dict) or decision.get("action") != "tool_call":
        return
    if not _router_is_explicit_jobs_listing(context.user_message):
        return
    if decision.get("tool") != "list_jobs":
        decision["tool"] = "list_jobs"
        decision["arguments"] = {}


def _normalize_result_evidence_tool(payload: object, context: MainModelContext) -> None:
    """Map internal result tools back to the graph's result business action."""

    if not isinstance(payload, dict):
        return
    decision = payload.get("decision")
    if not isinstance(decision, dict) or decision.get("action") != "tool_call":
        return
    if decision.get("tool") not in {"query_result_evidence", "query_artifact"}:
        return
    arguments = decision.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    job_id = decision.get("job_id") or arguments.get("job_id")
    if not isinstance(job_id, str) or not job_id.strip():
        if len(context.fact_index.job_artifacts) == 1:
            job_id = next(iter(context.fact_index.job_artifacts))
    artifact = arguments.get("artifact")
    if not isinstance(artifact, str) or not artifact.strip():
        artifacts = context.fact_index.job_artifacts.get(job_id, []) if isinstance(job_id, str) else []
        if len(artifacts) == 1:
            artifact = artifacts[0]
    if not isinstance(job_id, str) or not job_id.strip() or not isinstance(artifact, str) or not artifact.strip():
        return
    query: dict[str, object] = {"artifact": artifact.strip()}
    for key in ("field_path", "filters", "sort", "limit", "resolve_entity"):
        if key in arguments:
            query[key] = arguments[key]
    if "resolve_entity" not in query:
        raw_query = arguments.get("query")
        if isinstance(raw_query, str) and raw_query.strip():
            query["resolve_entity"] = raw_query.strip()
    decision["action"] = "query_result"
    decision["job_id"] = job_id.strip()
    decision["result_query"] = query
    decision["tool"] = None
    decision["arguments"] = {}


def _normalize_explicit_business_actions(payload: object, context: MainModelContext) -> None:
    """Recover obvious status/analysis actions that models emit as tools."""

    if not isinstance(payload, dict):
        return
    decision = payload.get("decision")
    if not isinstance(decision, dict) or decision.get("action") != "tool_call":
        return
    arguments = decision.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    tool = decision.get("tool")
    if tool == "get_jobs_status":
        if not _router_is_explicit_job_status_request(context.user_message):
            return
        job_id = decision.get("job_id") or arguments.get("job_id")
        decision["action"] = "get_job"
        decision["job_id"] = job_id if isinstance(job_id, str) and job_id.strip() else None
        decision["tool"] = None
        decision["arguments"] = {}
        return
    if tool == "list_jobs" and _router_is_explicit_job_status_request(context.user_message):
        if len(context.conversation_memory.recent_job_ids) > 1:
            decision["action"] = "get_job"
            decision["job_id"] = None
            decision["tool"] = None
            decision["arguments"] = {}
            return
    if tool not in {"describe_metadata", "enumerate_contrasts", "list_jobs"}:
        return
    message = context.user_message.casefold()
    analysis_type = next(
        (name for name in ("DEG", "DEM", "GMA") if name.casefold() in message),
        None,
    )
    if analysis_type is None:
        if "metabol" in message:
            analysis_type = "DEM"
        elif "gene" in message or "expression" in message:
            analysis_type = "DEG"
    if analysis_type is None or not _router_is_explicit_analysis_request(message):
        return
    proposal_values: dict[str, object] = {"analysis_type": analysis_type}
    fields = context.fact_index.metadata_fields
    levels = context.fact_index.metadata_levels
    compare_field = next(
        (field for field in fields if field in levels and field != "sample_id"),
        None,
    )
    if compare_field:
        proposal_values["compare_field"] = compare_field
        known_levels = list(levels.get(compare_field, {}))
        mentioned = [
            level for level in known_levels
            if level.casefold() in message
        ]
        if len(mentioned) >= 2:
            proposal_values["tested_level"] = mentioned[0]
            proposal_values["reference_level"] = mentioned[1]
        proposal_values["scope"] = {"mode": "all"}
    decision["action"] = "propose_plan" if "plan" in message else "run_analysis"
    decision["analysis_type"] = analysis_type
    decision["proposal"] = proposal_values
    decision["tool"] = None
    decision["arguments"] = {}


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


_GRAPH_MAIN_SYSTEM_PROMPT = (
    "You are OmicsPrism Copilot. Use the supplied tools for read-only data access "
    "and return a concise final answer when no tool is needed. A tool call may "
    "invoke only the read-only tools "
    "describe_metadata, enumerate_contrasts, list_jobs, describe_artifacts, or "
    "query_artifact; provide typed arguments and wait for its observation before "
    "deciding. Use answer, grounded_answer, or ask_user as terminal LoopExit actions. "
    "Route general knowledge to answer; dataset inspection or DEG/DEM/GMA requests "
    "to inspect_dataset, run_analysis, or propose_plan; existing Job status "
    "or evidence questions to get_job or query_result. Do not put action-specific "
    "For legacy structured final decisions, do not put action-specific "
    "fields in decision.arguments: arguments is only for tool_call. For analysis "
    "actions put candidates in decision.proposal, for example run_analysis uses "
    "{analysis_type: 'DEG', compare_field: 'treatment', tested_level: 'salt', "
    "reference_level: 'control', scope: {mode: 'all'}}. For get_job put the id in "
    "decision.job_id. For query_result put job_id and a complete decision.result_query "
    "with artifact and, when applicable, resolve_entity. AnalysisProposal values are "
    "candidates only and must use observed dataset roles and explicit user language. "
    "When discussing supported analyses, use fact_index.analysis_capabilities: "
    "an analysis is runnable only when its missing-role list is empty. "
    "When the context has exactly one in-scope Job and one artifact, a request such "
    "as 'Show GeneA result' or 'What is the GeneB fold change?' must use query_result "
    "directly; do not ask for clarification. For a follow-up such as 'make it shorter' "
    "or 'correct that', use recent_messages and answer the follow-up directly. "
    "Only pending_analysis with status=active is resumable. When it is active, "
    "treat the latest user message as a possible answer to that analysis clarification "
    "unless the user changes the dataset or asks an unrelated question; in the first "
    "case return the same analysis action with a completed proposal. Pending records "
    "with status=consumed, superseded, expired, or cancelled are historical context only and "
    "must not trigger analysis recovery. "
    "If tool_repetition_guidance is present, treat it as the latest tool result: do not "
    "select tool_call; choose only answer, ask_user, or grounded_answer. "
    "For 'List available jobs' or equivalent requests, you MUST first return "
    "{decision: {action: 'tool_call', tool: 'list_jobs', arguments: {}}, answer: null}. "
    "Never answer the job list from memory or context; the list_jobs observation "
    "is the only source of truth. "
    "If several Jobs are in context and the user asks for status without naming one, "
    "choose get_job with job_id null so the result node can ask which Job. "
    "A grounded_answer must cite the artifact, checksum, and row IDs from the latest "
    "successful query observation; never invent citations or numeric values. "
    "Never claim a dataset fact, Job, artifact, entity, or numeric result that is absent "
    "from the bounded context. "
    "Always respond in the same language as the user's most recent message. "
    "When returning a legacy structured final decision, action=answer requires a "
    "concise non-empty answer; action=ask_user requires question. "
    "Do not decide validation, ownership, ambiguity, or "
    "execution success."
)

_QA_SYSTEM_PROMPT = (
    "You are the OmicsPrism general QA agent. Answer general questions and use only "
    "the supplied recent messages and dataset role names. Do not infer metadata fields, "
    "analysis parameters, Jobs, artifacts, or numeric results that are not present. "
    "Return one typed decision: answer for a direct response, ask_user when the user "
    "must clarify, or reroute when the request belongs to analysis or result QA. "
    "Use the same language as the user's latest message."
)

_ANALYSIS_SYSTEM_PROMPT = (
    "You are the OmicsPrism analysis agent. Use the bounded fact_index metadata fields, "
    "metadata levels, dataset roles, pending_analysis, and decision_ledger to propose "
    "analysis decisions. Infer compare_field and scope only from observed metadata and "
    "explicit user language. Return inspect_dataset, propose_plan, or run_analysis for "
    "analysis work, ask_user for a missing requirement, or reroute for a general or "
    "result question. Do not claim validation or submit a Job. Use the user's language."
)

_RESULT_QA_SYSTEM_PROMPT = (
    "You are the OmicsPrism result QA agent. Use only the supplied ownership-bound Job "
    "references, in-scope Job IDs, and artifact index. Return query_result for an "
    "evidence request, get_job for Job status, grounded_answer only when evidence is "
    "available, ask_user when a Job must be selected, or reroute for analysis or general "
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
        "schema_version": "analysis-model-output.v1",
        "tool_names": {"describe_metadata", "enumerate_contrasts"},
    },
    AgentRole.RESULT_QA: {
        "system_prompt": _RESULT_QA_SYSTEM_PROMPT,
        "output_model": ResultQaModelOutput,
        "schema_name": "result_qa_model_output",
        "schema_version": "result-qa-model-output.v1",
        "tool_names": {"list_jobs", "describe_artifacts", "query_artifact"},
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
