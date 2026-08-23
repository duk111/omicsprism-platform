from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..models import AnalysisType
from .dataset_profile import DatasetProfile, MetadataProfile
from .schemas import InputInspectionSummary


AnalysisName = Literal["DEG", "DEM", "GMA"]
ParamValue = str | int | float | bool | None


class AnalysisProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis_type: AnalysisName | None = None
    compare_field: str | None = None
    tested_level: str | None = None
    reference_level: str | None = None
    same_fields: dict[str, str] = Field(default_factory=dict)
    requested_params: dict[str, ParamValue] = Field(default_factory=dict, max_length=32)

    @classmethod
    def from_legacy(
        cls,
        analysis_type: AnalysisType | str,
        params: Mapping[str, object],
    ) -> "AnalysisProposal":
        name = _analysis_name(analysis_type)
        same_fields = _same_fields(params.get("same_fields"))
        tested = str(params.get("tested_level") or params.get("tested_levels") or "").strip() or None
        reference = str(params.get("reference_level") or "").strip() or None
        allowed: dict[str, ParamValue] = {}
        for key, value in params.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                allowed[str(key)] = value
        return cls(
            analysis_type=name,
            compare_field=_optional_text(params.get("compare_field")),
            tested_level=tested,
            reference_level=reference,
            same_fields=same_fields,
            requested_params=allowed,
        )


class ContrastSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compare_field: str
    tested_level: str
    reference_level: str
    same_fields: dict[str, str] = Field(default_factory=dict)


class _ContrastParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contrast: ContrastSpec
    min_replicates: int = Field(default=2, ge=1)

    def legacy_params(self) -> dict[str, ParamValue]:
        values: dict[str, ParamValue] = {
            "compare_field": self.contrast.compare_field,
            "tested_levels": self.contrast.tested_level,
            "reference_level": self.contrast.reference_level,
            "same_fields": ",".join(self.contrast.same_fields),
            "min_replicates": self.min_replicates,
        }
        values.update(self._legacy_extra_params())
        return values

    def _legacy_extra_params(self) -> dict[str, ParamValue]:
        return {}


class DEGParams(_ContrastParams):
    analysis_type: Literal["DEG"] = "DEG"
    padj_cutoff: float = Field(default=0.05, ge=0, le=1)
    log2fc_cutoff: float = Field(default=1.0, ge=0)
    min_total_count: int = Field(default=10, ge=0)
    normalize: bool = True
    filter_low_expression: bool = True

    def _legacy_extra_params(self) -> dict[str, ParamValue]:
        return {
            "padj_cutoff": self.padj_cutoff,
            "log2fc_cutoff": self.log2fc_cutoff,
            "min_total_count": self.min_total_count,
            "normalize": self.normalize,
            "filter_low_expression": self.filter_low_expression,
        }


class DEMParams(_ContrastParams):
    analysis_type: Literal["DEM"] = "DEM"
    padj_cutoff: float = Field(default=0.05, ge=0, le=1)
    log2fc_cutoff: float = Field(default=1.0, ge=0)
    vip_cutoff: float = Field(default=1.0, ge=0)
    pseudocount: float = Field(default=1e-9, ge=0)
    max_missing_fraction: float = Field(default=0.5, ge=0, le=1)
    impute_method: str = "half-min"
    normalize: bool = True
    log_transform: bool = True
    n_orthogonal_components: int = Field(default=1, ge=1)

    def _legacy_extra_params(self) -> dict[str, ParamValue]:
        return {
            "padj_cutoff": self.padj_cutoff,
            "log2fc_cutoff": self.log2fc_cutoff,
            "vip_cutoff": self.vip_cutoff,
            "pseudocount": self.pseudocount,
            "max_missing_fraction": self.max_missing_fraction,
            "impute_method": self.impute_method,
            "normalize": self.normalize,
            "log_transform": self.log_transform,
            "n_orthogonal_components": self.n_orthogonal_components,
        }


class GMAParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis_type: Literal["GMA"] = "GMA"
    fdr_cutoff: float = Field(default=0.05, ge=0, le=1)
    enable_modules: bool = True
    trans_log2: bool = True
    metab_log2: bool = True
    max_missing_fraction: float = Field(default=0.5, ge=0, le=1)

    def legacy_params(self) -> dict[str, ParamValue]:
        return self.model_dump(exclude={"analysis_type"})


AnalysisParams: TypeAlias = Annotated[
    DEGParams | DEMParams | GMAParams,
    Field(discriminator="analysis_type"),
]


class MissingParam(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    options: list[str] = Field(default_factory=list, max_length=20)
    reason: str


class ResolvedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis_type: AnalysisName | None
    params: AnalysisParams | None
    missing: list[MissingParam] = Field(default_factory=list)
    inference_note: str | None = None
    partial_params: dict[str, ParamValue] = Field(default_factory=dict, max_length=32)

    def legacy_params(self) -> dict[str, ParamValue]:
        if self.params is not None:
            return self.params.legacy_params()
        return dict(self.partial_params)

    @property
    def clarification(self) -> str | None:
        if not self.missing:
            return None
        details = "；".join(
            f"{item.reason}"
            + (f"（候选：{', '.join(item.options)}）" if item.options else "")
            for item in self.missing[:3]
        )
        return f"生成分析计划前需要补充参数。{details}"


def resolve_analysis_request(
    user_message: str,
    profiles: list[DatasetProfile],
    llm_proposal: AnalysisProposal | None = None,
    prior_params: AnalysisParams | None = None,
) -> ResolvedRequest:
    """Resolve model candidates against observed metadata facts.

    The resolver enumerates legal contrasts from metadata and returns a
    missing-param request whenever more than one legal interpretation remains.
    It never fuzzy-corrects an unknown field or level.
    """

    proposal = llm_proposal or AnalysisProposal()
    analysis_type = proposal.analysis_type or _analysis_name_from_prior(prior_params)
    if analysis_type is None:
        return ResolvedRequest(
            analysis_type=None,
            params=None,
            missing=[MissingParam(field="analysis_type", reason="无法从当前请求确定分析类型")],
        )
    metadata = next((item for item in profiles if isinstance(item, MetadataProfile)), None)
    if metadata is None:
        return ResolvedRequest(
            analysis_type=analysis_type,
            params=None,
            missing=[MissingParam(field="metadata", reason="缺少 metadata/group 数据")],
        ) if analysis_type in {"DEG", "DEM"} else _build_params(analysis_type, proposal.requested_params, None)

    merged = _merge_params(proposal, prior_params)
    if user_message:
        merged = _apply_message_levels(merged, user_message, metadata)
    if analysis_type == "GMA":
        return _build_params(analysis_type, merged, None)

    min_replicates = _positive_int(merged.get("min_replicates"), 2)
    candidates = _enumerate_candidates(metadata, merged, min_replicates)
    explicit = _has_explicit_contrast(merged)
    if explicit:
        if not candidates:
            return _missing_for_invalid_candidate(analysis_type, metadata, merged, min_replicates)
        if len(candidates) > 1:
            return _ambiguous_request(analysis_type, merged, candidates)
        return _build_resolved(analysis_type, merged, candidates[0], metadata)
    if len(candidates) == 1:
        return _build_resolved(analysis_type, merged, candidates[0], metadata)
    if len(candidates) > 1:
        return _ambiguous_request(analysis_type, merged, candidates)
    return _missing_for_unresolved(analysis_type, metadata, merged, min_replicates)


def profiles_from_input_summaries(summaries: Sequence[InputInspectionSummary]) -> list[DatasetProfile]:
    """Bridge the legacy bounded context summary into the new profile contract."""

    profiles: list[DatasetProfile] = []
    for summary in summaries:
        if summary.field in {"metadata", "group"}:
            profiles.append(MetadataProfile(
                role=summary.field,
                columns=list(summary.columns),
                levels={
                    group.column: {item.value: item.count for item in group.values}
                    for group in summary.group_levels
                },
                sample_ids=[],
                rows=summary.raw_rows,
                alignment={},
            ))
    return profiles


def _enumerate_candidates(
    metadata: MetadataProfile,
    params: Mapping[str, object],
    min_replicates: int,
) -> list[tuple[str, str, str, dict[str, str]]]:
    compare_field = _optional_text(params.get("compare_field"))
    tested = _optional_text(params.get("tested_level") or params.get("tested_levels"))
    reference = _optional_text(params.get("reference_level"))
    fixed_same = _same_fields(params.get("same_fields"))
    candidates: list[tuple[str, str, str, dict[str, str]]] = []
    for field, raw_values in metadata.levels.items():
        if compare_field and field != compare_field:
            continue
        values = [value for value, count in raw_values.items() if value and count >= min_replicates]
        if tested and tested not in values:
            continue
        if reference and reference not in values:
            continue
        refs = [value for value in values if _is_reference_level(value)]
        reference_values = [reference] if reference else (refs if len(refs) == 1 else [])
        tested_values = [tested] if tested else [value for value in values if value not in reference_values]
        if tested is None and len(tested_values) != 1:
            continue
        for ref in reference_values:
            for test in tested_values:
                if test == ref:
                    continue
                same_options = _same_field_options(metadata, field, test, ref, fixed_same, min_replicates)
                for same_fields in same_options:
                    candidates.append((field, test, ref, same_fields))
    return _dedupe_candidates(candidates)


def _same_field_options(
    metadata: MetadataProfile,
    compare_field: str,
    tested: str,
    reference: str,
    fixed_same: dict[str, str],
    min_replicates: int,
) -> list[dict[str, str]]:
    requested_fields = list(fixed_same)
    if any(field == compare_field or field not in metadata.levels for field in requested_fields):
        return []
    fixed_constraints = {field: value for field, value in fixed_same.items() if value}
    dimension_only = bool(requested_fields) and not fixed_constraints
    if requested_fields and not dimension_only and len(fixed_constraints) != len(requested_fields):
        return []
    if any(value not in metadata.levels[field] for field, value in fixed_constraints.items()):
        return []

    secondary_fields = requested_fields or [
        field
        for field in metadata.levels
        if field not in {compare_field, "sample_id"}
    ]
    if not secondary_fields:
        return [{}]
    if metadata.rows is None:
        # Aggregate level counts cannot prove per-stratum replicate counts.
        return []

    strata: dict[tuple[str, ...], list[list[str]]] = {}
    for row in metadata.rows:
        if _row_value(row, metadata, compare_field) not in {tested, reference}:
            continue
        if any(_row_value(row, metadata, field) != value for field, value in fixed_constraints.items()):
            continue
        key = tuple(_row_value(row, metadata, field) for field in secondary_fields)
        if all(key):
            strata.setdefault(key, []).append(row)

    options: list[dict[str, str]] = []
    for key, rows in strata.items():
        if sum(_row_value(row, metadata, compare_field) == tested for row in rows) < min_replicates:
            continue
        if sum(_row_value(row, metadata, compare_field) == reference for row in rows) < min_replicates:
            continue
        options.append(dict(zip(secondary_fields, key)))
    if dimension_only:
        return [dict(fixed_same)] if options else []
    return options


def _build_resolved(
    analysis_type: AnalysisName,
    params: Mapping[str, object],
    candidate: tuple[str, str, str, dict[str, str]],
    metadata: MetadataProfile,
) -> ResolvedRequest:
    field, tested, reference, same_fields = candidate
    if not same_fields:
        same_fields = _same_fields(params.get("same_fields"))
    merged = dict(params)
    merged.update({
        "compare_field": field,
        "tested_level": tested,
        "tested_levels": tested,
        "reference_level": reference,
        "same_fields": same_fields,
    })
    built = _build_params(analysis_type, merged, (field, tested, reference, same_fields))
    if built.params is not None:
        built.inference_note = (
            f"参数依据 metadata 的 {field} 分组水平 {tested} 与 {reference}"
            + (f"，并限定 {', '.join(f'{key}={value}' for key, value in same_fields.items())}" if same_fields else "")
            + "；请在确认前核对。"
        )
    return built


def _build_params(
    analysis_type: AnalysisName,
    params: Mapping[str, object],
    candidate: tuple[str, str, str, dict[str, str]] | None,
) -> ResolvedRequest:
    merged = dict(params)
    if candidate is not None:
        field, tested, reference, same_fields = candidate
        merged.update({"compare_field": field, "tested_levels": tested, "reference_level": reference, "same_fields": same_fields})
        contrast = ContrastSpec(compare_field=field, tested_level=tested, reference_level=reference, same_fields=same_fields)
    elif analysis_type in {"DEG", "DEM"}:
        return ResolvedRequest(analysis_type=analysis_type, params=None, partial_params=_scalar_params(merged))
    try:
        if analysis_type == "DEG":
            value: AnalysisParams = DEGParams(
                contrast=contrast,
                min_replicates=_positive_int(merged.get("min_replicates"), 2),
                **_contrast_extras(merged, DEGParams),
            )  # type: ignore[arg-type]
        elif analysis_type == "DEM":
            value = DEMParams(
                contrast=contrast,
                min_replicates=_positive_int(merged.get("min_replicates"), 2),
                **_contrast_extras(merged, DEMParams),
            )  # type: ignore[arg-type]
        else:
            value = GMAParams(**_gma_extras(merged))
    except ValidationError as exc:
        field = str(exc.errors()[0].get("loc", ["params"])[-1])
        return ResolvedRequest(
            analysis_type=analysis_type,
            params=None,
            partial_params=_scalar_params(merged),
            missing=[MissingParam(field=field, reason=f"参数 {field} 不合法")],
        )
    return ResolvedRequest(analysis_type=analysis_type, params=value, partial_params=_scalar_params(merged))


def _missing_for_invalid_candidate(
    analysis_type: AnalysisName,
    metadata: MetadataProfile,
    params: Mapping[str, object],
    min_replicates: int,
) -> ResolvedRequest:
    field = _optional_text(params.get("compare_field"))
    if field and field not in metadata.levels:
        return ResolvedRequest(analysis_type=analysis_type, params=None, partial_params=_scalar_params(params), missing=[MissingParam(field="compare_field", options=list(metadata.levels)[:20], reason=f"metadata 中不存在列 {field}")])
    options = list(metadata.levels.get(field or "", {}))[:20]
    tested = _optional_text(params.get("tested_levels"))
    reference = _optional_text(params.get("reference_level"))
    observed = metadata.levels.get(field or "", {})
    same_fields = _same_fields(params.get("same_fields"))
    invalid_same = [
        f"{name}={value}"
        for name, value in same_fields.items()
        if name not in metadata.levels or (value and value not in metadata.levels[name])
    ]
    if invalid_same:
        reason = f"same_fields 不存在于真实 metadata：{', '.join(invalid_same)}"
    elif tested and tested not in observed:
        reason = f"metadata 列 {field} 中不存在 tested level {tested}"
    elif reference and reference not in observed:
        reason = f"metadata 列 {field} 中不存在 reference level {reference}"
    elif tested and reference and (observed[tested] < min_replicates or observed[reference] < min_replicates):
        reason = "模型给出的分组样本数不足 min_replicates，请重新选择分组"
    else:
        reason = "应用 same_fields 后分组样本数不足 min_replicates，无法形成合法 contrast"
    return ResolvedRequest(analysis_type=analysis_type, params=None, partial_params=_scalar_params(params), missing=[MissingParam(field="contrast", options=options, reason=reason)])


def _missing_for_unresolved(
    analysis_type: AnalysisName,
    metadata: MetadataProfile,
    params: Mapping[str, object],
    min_replicates: int,
) -> ResolvedRequest:
    fields = list(metadata.levels)[:20]
    field = _optional_text(params.get("compare_field"))
    if not field:
        return ResolvedRequest(analysis_type=analysis_type, params=None, partial_params=_scalar_params(params), missing=[MissingParam(field="compare_field", options=fields, reason="需要选择包含实验分组的 metadata 列")])
    if field not in metadata.levels:
        return ResolvedRequest(analysis_type=analysis_type, params=None, partial_params=_scalar_params(params), missing=[MissingParam(field="compare_field", options=fields, reason=f"metadata 中不存在列 {field}")])
    values = [value for value, count in metadata.levels.get(field, {}).items() if count >= min_replicates]
    tested = _optional_text(params.get("tested_levels"))
    reference = _optional_text(params.get("reference_level"))
    if tested and not reference:
        missing_field = "reference_level"
    elif reference and not tested:
        missing_field = "tested_level"
    else:
        refs = [value for value in values if _is_reference_level(value)]
        missing_field = "tested_level" if len(refs) == 1 and len(values) > 2 else "reference_level"
    return ResolvedRequest(analysis_type=analysis_type, params=None, partial_params=_scalar_params(params), missing=[MissingParam(field=missing_field, options=values[:20], reason=f"{field} 有多个合法水平，不能猜测比较设置")])


def _ambiguous_request(
    analysis_type: AnalysisName,
    params: Mapping[str, object],
    candidates: Sequence[tuple[str, str, str, dict[str, str]]],
) -> ResolvedRequest:
    options = [
        f"{field}: {tested} vs {reference}"
        + (f" ({', '.join(f'{key}={value}' for key, value in same.items())})" if same else "")
        for field, tested, reference, same in candidates[:20]
    ]
    return ResolvedRequest(
        analysis_type=analysis_type,
        params=None,
        partial_params=_scalar_params(params),
        missing=[MissingParam(field="contrast", options=options, reason="存在多个合法 contrast，必须由用户明确选择")],
    )


def _merge_params(proposal: AnalysisProposal, prior_params: AnalysisParams | None) -> dict[str, object]:
    merged: dict[str, object] = {}
    if prior_params is not None and (proposal.analysis_type is None or proposal.analysis_type == prior_params.analysis_type):
        merged.update(prior_params.legacy_params())
    merged.update(proposal.requested_params)
    if proposal.compare_field is not None:
        merged["compare_field"] = proposal.compare_field
    if proposal.tested_level is not None:
        merged["tested_levels"] = proposal.tested_level
    if proposal.reference_level is not None:
        merged["reference_level"] = proposal.reference_level
    if proposal.same_fields:
        merged["same_fields"] = proposal.same_fields
    return merged


def _apply_message_levels(params: dict[str, object], message: str, metadata: MetadataProfile) -> dict[str, object]:
    mentioned = {
        column: [value for value in values if _mentions_value(message, value)]
        for column, values in metadata.levels.items()
        if column != "sample_id"
    }
    mentioned = {column: values for column, values in mentioned.items() if values}
    pair_columns = [column for column, values in mentioned.items() if len(values) >= 2]
    old_field = _optional_text(params.get("compare_field"))
    if len(pair_columns) == 1:
        field = pair_columns[0]
        if old_field and old_field != field:
            params.pop("tested_levels", None)
            params.pop("reference_level", None)
            params["same_fields"] = {}
        params["compare_field"] = field
    else:
        field = old_field

    if field and field in mentioned:
        values = mentioned[field]
        references = [value for value in values if _is_reference_level(value)]
        tested_values = [value for value in values if value not in references]
        explicit_pair = field in pair_columns
        if len(references) == 1 and (explicit_pair or not _optional_text(params.get("reference_level"))):
            params["reference_level"] = references[0]
        elif len(references) > 1:
            params.pop("reference_level", None)
        if len(tested_values) == 1 and (explicit_pair or not _optional_text(params.get("tested_levels"))):
            params["tested_levels"] = tested_values[0]
        elif len(tested_values) > 1:
            params.pop("tested_levels", None)

    same_fields = _same_fields(params.get("same_fields"))
    for column, values in mentioned.items():
        if column == field:
            continue
        if len(values) == 1:
            same_fields[column] = values[0]
        else:
            same_fields.pop(column, None)
    if same_fields:
        params["same_fields"] = same_fields
    return params


def _mentions_value(message: str, value: str) -> bool:
    lowered_message = message.casefold()
    lowered_value = value.casefold().strip()
    if not lowered_value:
        return False
    if any(ord(character) > 127 for character in lowered_value):
        return lowered_value in lowered_message
    return re.search(rf"(?<![a-z0-9_]){re.escape(lowered_value)}(?![a-z0-9_])", lowered_message) is not None


def _row_value(row: list[str], metadata: MetadataProfile, field: str) -> str:
    try:
        index = metadata.columns.index(field)
    except ValueError:
        return ""
    return row[index].strip() if index < len(row) else ""


def _dedupe_candidates(candidates: Sequence[tuple[str, str, str, dict[str, str]]]) -> list[tuple[str, str, str, dict[str, str]]]:
    result: list[tuple[str, str, str, dict[str, str]]] = []
    seen: set[tuple[str, str, str, tuple[tuple[str, str], ...]]] = set()
    for candidate in candidates:
        key = (*candidate[:3], tuple(sorted(candidate[3].items())))
        if key not in seen:
            seen.add(key)
            result.append(candidate)
    return result


def _has_explicit_contrast(params: Mapping[str, object]) -> bool:
    return all(_optional_text(params.get(key)) for key in ("compare_field", "tested_levels", "reference_level"))


def _contrast_extras(params: Mapping[str, object], model: type[_ContrastParams]) -> dict[str, object]:
    names = set(model.model_fields) - {"analysis_type", "contrast", "min_replicates"}
    return {name: params[name] for name in names if name in params}


def _gma_extras(params: Mapping[str, object]) -> dict[str, object]:
    names = set(GMAParams.model_fields) - {"analysis_type"}
    return {name: params[name] for name in names if name in params}


def _scalar_params(params: Mapping[str, object]) -> dict[str, ParamValue]:
    return {
        str(key): value
        for key, value in params.items()
        if isinstance(value, (str, int, float, bool)) or value is None
    }


def _same_fields(value: object) -> dict[str, str]:
    if isinstance(value, Mapping):
        return {str(key): str(item) for key, item in value.items() if str(key).strip()}
    if isinstance(value, str):
        return {item.strip(): "" for item in value.split(",") if item.strip()}
    if isinstance(value, (list, tuple, set)):
        return {str(item).strip(): "" for item in value if str(item).strip()}
    return {}


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _analysis_name(value: AnalysisType | str) -> AnalysisName:
    raw = value.value if isinstance(value, AnalysisType) else str(value)
    return {"deg": "DEG", "DEG": "DEG", "dem": "DEM", "DEM": "DEM", "gma": "GMA", "GMA": "GMA"}[raw]  # type: ignore[return-value]


def _analysis_name_from_prior(prior: AnalysisParams | None) -> AnalysisName | None:
    return prior.analysis_type if prior is not None else None


def _positive_int(value: object, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


_REFERENCE_LEVEL_MARKERS = {"control", "ctrl", "ck", "wt", "mock", "untreated", "对照", "空白", "野生型", "未处理"}


def _is_reference_level(value: str) -> bool:
    lowered = value.strip().casefold()
    tokens = {item for item in re.split(r"[^a-z0-9\u4e00-\u9fff]+", lowered) if item}
    return lowered in _REFERENCE_LEVEL_MARKERS or bool(tokens & _REFERENCE_LEVEL_MARKERS)
