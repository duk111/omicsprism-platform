from __future__ import annotations

import json
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .dataset_profile import MetadataProfile
from .param_resolver import ScopeSpec


ScalarValue = str | int | float | bool | None


class FactIndex(BaseModel):
    """Bounded deterministic facts that are safe to place in a model prompt."""

    model_config = ConfigDict(extra="forbid")

    context_version: str = Field(min_length=1, max_length=80)
    truncated: bool = False
    dataset_roles: list[str] = Field(default_factory=list, max_length=6)
    metadata_fields: list[str] = Field(default_factory=list, max_length=20)
    metadata_levels: dict[str, dict[str, int]] = Field(default_factory=dict, max_length=20)
    sample_count: int = Field(default=0, ge=0)
    alignment: dict[str, str] = Field(default_factory=dict, max_length=12)
    job_artifacts: dict[str, list[str]] = Field(default_factory=dict, max_length=20)


class DecisionLedger(BaseModel):
    """Non-summarized record of decisions already made in the current thread."""

    model_config = ConfigDict(extra="forbid")

    context_version: str = Field(min_length=1, max_length=80)
    truncated: bool = False
    analysis_type: Literal["DEG", "DEM", "GMA"] | None = None
    compare_field: str | None = Field(default=None, max_length=200)
    tested_level: str | None = Field(default=None, max_length=200)
    reference_level: str | None = Field(default=None, max_length=200)
    scope: ScopeSpec | None = None
    fixed_conditions: dict[str, str] = Field(default_factory=dict, max_length=16)
    blocking_fields: list[str] = Field(default_factory=list, max_length=16)
    parameter_values: dict[str, ScalarValue] = Field(default_factory=dict, max_length=32)
    rejected_candidates: list[str] = Field(default_factory=list, max_length=20)


class WorkingSetItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["job", "evidence", "message", "tool"]
    text: str = Field(min_length=1, max_length=1000)
    truncated: bool = False


class WorkingSet(BaseModel):
    """Recent tool/result material with a strict item and character budget."""

    model_config = ConfigDict(extra="forbid")

    context_version: str = Field(min_length=1, max_length=80)
    truncated: bool = False
    items: list[WorkingSetItem] = Field(default_factory=list, max_length=3)


class MainModelContext(BaseModel):
    """Prompt-safe context assembled from bounded state and deterministic facts."""

    model_config = ConfigDict(extra="forbid")

    # Ownership and correlation identifiers are available to tracing code but
    # must never be serialized into the model prompt.
    trace_id: str = Field(default="trace-local", min_length=1, max_length=200, exclude=True)
    thread_id: str = Field(default="thread-local", min_length=1, max_length=200, exclude=True)
    turn_id: str = Field(default="turn-local", min_length=1, max_length=200, exclude=True)
    run_id: str = Field(default="run-local", min_length=1, max_length=200, exclude=True)
    user_id: str = Field(default="user-local", min_length=1, max_length=200, exclude=True)
    user_message: str = Field(min_length=1, max_length=4000)
    conversation_summary: str | None = Field(default=None, max_length=1200)
    fact_index: FactIndex
    decision_ledger: DecisionLedger
    working_set: WorkingSet


class ContextAssembler:
    """Build the sole prompt context from GraphState without raw dataset payloads."""

    _MAX_METADATA_FIELDS = 20
    _MAX_LEVELS_PER_FIELD = 12
    _MAX_JOB_ARTIFACTS = 20
    _MAX_WORKING_ITEMS = 3
    _MAX_WORKING_ITEM_CHARS = 1000

    def assemble(self, state: object) -> MainModelContext:
        fact_index = self._fact_index(state)
        ledger = self._decision_ledger(state)
        working_set = self._working_set(state)
        summary = getattr(state, "conversation_summary", None)
        if summary:
            summary = str(summary)[:1200]
        return MainModelContext(
            trace_id=str(getattr(state, "trace_id", "") or "trace-local"),
            thread_id=str(getattr(state, "thread_id", "") or "thread-local"),
            turn_id=str(getattr(state, "turn_id", "") or "turn-local"),
            run_id=str(getattr(state, "run_id", "") or "run-local"),
            user_id=str(getattr(state, "user_id", "") or "user-local"),
            user_message=str(getattr(state, "user_message", "")),
            conversation_summary=summary,
            fact_index=fact_index,
            decision_ledger=ledger,
            working_set=working_set,
        )

    def _fact_index(self, state: object) -> FactIndex:
        roles: list[str] = []
        metadata_fields: list[str] = []
        metadata_levels: dict[str, dict[str, int]] = {}
        alignment: dict[str, str] = {}
        sample_count = 0
        truncated = False
        for item in getattr(state, "dataset_profiles", []) or []:
            profile = getattr(item, "profile", item)
            role = str(getattr(profile, "role", ""))
            if role and role not in roles and len(roles) < 6:
                roles.append(role)
            if not isinstance(profile, MetadataProfile):
                continue
            if sample_count == 0:
                sample_count = len(profile.sample_ids)
            for field in profile.columns:
                if field not in metadata_fields:
                    if len(metadata_fields) >= self._MAX_METADATA_FIELDS:
                        truncated = True
                        continue
                    metadata_fields.append(field)
                levels = profile.levels.get(field)
                if levels is not None and field in metadata_fields and field not in metadata_levels:
                    values = dict(list(levels.items())[: self._MAX_LEVELS_PER_FIELD])
                    metadata_levels[field] = values
                    truncated = truncated or len(levels) > self._MAX_LEVELS_PER_FIELD
            alignment.update({str(key): str(value) for key, value in profile.alignment.items()})
        job_artifacts: dict[str, list[str]] = {}
        summary = getattr(state, "job_summary", None)
        if summary is not None:
            job_id = str(getattr(summary, "job_id", ""))
            if job_id:
                artifacts = [str(item) for item in getattr(summary, "artifacts", [])]
                job_artifacts[job_id] = artifacts[: self._MAX_JOB_ARTIFACTS]
                truncated = truncated or len(artifacts) > self._MAX_JOB_ARTIFACTS
        payload = {
            "roles": roles,
            "fields": metadata_fields,
            "levels": metadata_levels,
            "sample_count": sample_count,
            "alignment": alignment,
            "job_artifacts": job_artifacts,
        }
        return FactIndex(
            context_version=_version("facts", payload),
            truncated=truncated,
            dataset_roles=roles,
            metadata_fields=metadata_fields,
            metadata_levels=metadata_levels,
            sample_count=sample_count,
            alignment=alignment,
            job_artifacts=job_artifacts,
        )

    def _decision_ledger(self, state: object) -> DecisionLedger:
        analysis_type: str | None = None
        compare_field = tested_level = reference_level = None
        scope: ScopeSpec | None = None
        parameter_values: dict[str, ScalarValue] = {}
        resolved = getattr(state, "resolved_request", None)
        params = getattr(resolved, "params", None)
        if params is not None:
            analysis_type = str(getattr(params, "analysis_type", "")) or None
            contrast = getattr(params, "contrast", None)
            if contrast is not None:
                compare_field = contrast.compare_field
                tested_level = contrast.tested_level
                reference_level = contrast.reference_level
                scope = contrast.scope
            for name, value in params.model_dump(mode="python").items():
                if isinstance(value, (str, int, float, bool)) or value is None:
                    parameter_values[name] = value
        focus = getattr(state, "focus", None)
        if analysis_type is None:
            candidate_type = getattr(focus, "draft_analysis_type", None)
            if candidate_type in {"DEG", "DEM", "GMA"}:
                analysis_type = candidate_type
        for source in (getattr(focus, "draft_params", {}), getattr(focus, "preferences", {})):
            for name, value in (source or {}).items():
                if isinstance(value, (str, int, float, bool)) or value is None:
                    parameter_values.setdefault(str(name), value)
        rejected: list[str] = []
        missing = getattr(resolved, "missing", []) or []
        for item in missing:
            if getattr(item, "field", None) == "contrast":
                rejected.extend(str(option) for option in getattr(item, "options", [])[:20])
        fixed = dict(scope.fixed_filters) if scope is not None and scope.mode == "fixed" else {}
        blocking = list(scope.blocking_fields) if scope is not None and scope.mode == "stratified" else []
        payload = {
            "analysis_type": analysis_type,
            "compare_field": compare_field,
            "tested_level": tested_level,
            "reference_level": reference_level,
            "scope": scope.model_dump(mode="python") if scope is not None else None,
            "fixed": fixed,
            "blocking": blocking,
            "parameters": parameter_values,
            "rejected": rejected,
        }
        return DecisionLedger(
            context_version=_version("ledger", payload),
            analysis_type=analysis_type,  # type: ignore[arg-type]
            compare_field=compare_field,
            tested_level=tested_level,
            reference_level=reference_level,
            scope=scope,
            fixed_conditions=fixed,
            blocking_fields=blocking,
            parameter_values=parameter_values,
            rejected_candidates=rejected,
        )

    def _working_set(self, state: object) -> WorkingSet:
        items: list[WorkingSetItem] = []
        for observation in (getattr(state, "tool_observations", []) or [])[-self._MAX_WORKING_ITEMS :]:
            tool = str(getattr(observation, "tool", "tool"))
            summary = str(getattr(observation, "summary", ""))
            if summary:
                items.append(WorkingSetItem(kind="tool", text=f"{tool}: {summary}"[: self._MAX_WORKING_ITEM_CHARS]))
        for job in (getattr(state, "recent_jobs", []) or [])[-self._MAX_WORKING_ITEMS :]:
            if len(items) >= self._MAX_WORKING_ITEMS:
                break
            job_id = str(getattr(job, "job_id", ""))
            if job_id:
                items.append(WorkingSetItem(kind="job", text=f"Job {job_id}"))
        summary = getattr(state, "job_summary", None)
        if summary is not None and len(items) < self._MAX_WORKING_ITEMS:
            status = str(getattr(summary, "status", ""))
            progress = getattr(summary, "progress", None)
            text = f"Job {summary.job_id}: {status}"
            if progress is not None:
                text += f" ({progress}%)"
            items.append(WorkingSetItem(kind="job", text=text[: self._MAX_WORKING_ITEM_CHARS]))
        answer = getattr(state, "grounded_answer", None)
        if answer is not None and len(items) < self._MAX_WORKING_ITEMS:
            claims = getattr(answer, "claims", [])[:3]
            text = "\n".join(str(getattr(claim, "text", "")) for claim in claims if getattr(claim, "text", ""))
            if text:
                items.append(WorkingSetItem(kind="evidence", text=text[: self._MAX_WORKING_ITEM_CHARS]))
        response = getattr(state, "response_text", None)
        if response and len(items) < self._MAX_WORKING_ITEMS:
            items.append(WorkingSetItem(kind="message", text=str(response)[: self._MAX_WORKING_ITEM_CHARS]))
        payload = [item.model_dump(mode="python") for item in items]
        return WorkingSet(
            context_version=_version("working", payload),
            items=items,
            truncated=(
                len(getattr(state, "recent_jobs", []) or []) > self._MAX_WORKING_ITEMS
                or len(getattr(state, "tool_observations", []) or []) > self._MAX_WORKING_ITEMS
            ),
        )


def _version(prefix: str, payload: object) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}.v1:{sha256(canonical.encode('utf-8')).hexdigest()[:16]}"
