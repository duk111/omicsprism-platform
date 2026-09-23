"""Resolve ordinary-language replies to an active analysis clarification.

The resolver is deliberately a small protocol boundary.  It may be backed by
an LLM, but its output is only a proposal: the analysis resolver and dataset
validation remain the authority before a plan can be confirmed or submitted.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .context import RecentMessage
from .param_resolver import DEGParams, DEMParams, GMAParams
from .schemas import AgentParamValue


ClarificationIntent = Literal[
    "edit_params",
    "confirm",
    "cancel",
    "param_question",
    "unrelated",
]


class ClarificationOption(BaseModel):
    """A stable, bounded candidate shown to the clarification resolver."""

    model_config = ConfigDict(extra="forbid")

    option_id: str = Field(min_length=1, max_length=160)
    label: str = Field(min_length=1, max_length=400)
    # This is an internal deterministic mapping.  ClarificationResolverInput
    # strips it before building the model prompt; GraphState retains it for
    # checkpoint/resume safety.
    proposal_patch: dict[str, object] = Field(default_factory=dict, max_length=16)


class ParamFieldSpec(BaseModel):
    """Human-readable parameter metadata derived from a Pydantic field."""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1, max_length=100)
    value_type: Literal["string", "integer", "number", "boolean", "unknown"]
    default: AgentParamValue = None
    ge: float | int | None = None
    le: float | int | None = None
    description: str = Field(min_length=1, max_length=300)


class ClarificationResolverInput(BaseModel):
    """Bounded facts supplied to the clarification sub-agent."""

    model_config = ConfigDict(extra="forbid")

    user_reply: str = Field(min_length=1, max_length=4000)
    pending_question: str | None = Field(default=None, max_length=1200)
    options: list[ClarificationOption] = Field(default_factory=list, max_length=20)
    param_spec: dict[str, ParamFieldSpec] = Field(default_factory=dict, max_length=20)
    recent_messages: list[RecentMessage] = Field(default_factory=list, max_length=8)

    def model_dump(self, *args: object, **kwargs: object) -> dict[str, object]:
        """Serialize only the model-visible option ID and label."""

        payload = super().model_dump(*args, **kwargs)
        options = payload.get("options")
        if isinstance(options, list):
            payload["options"] = [
                {key: value for key, value in item.items() if key in {"option_id", "label"}}
                for item in options
                if isinstance(item, dict)
            ]
        return payload

    @classmethod
    def model_json_schema(cls, *args: object, **kwargs: object) -> dict[str, object]:
        schema = super().model_json_schema(*args, **kwargs)
        option_schema = schema.get("$defs", {}).get("ClarificationOption")
        if isinstance(option_schema, dict):
            properties = option_schema.get("properties")
            if isinstance(properties, dict):
                properties.pop("proposal_patch", None)
            required = option_schema.get("required")
            if isinstance(required, list) and "proposal_patch" in required:
                required.remove("proposal_patch")
        return schema


class ClarificationResolverOutput(BaseModel):
    """A classified reply and a candidate parameter patch."""

    model_config = ConfigDict(extra="forbid")

    intent: ClarificationIntent
    matched_option_id: str | None = Field(default=None, max_length=160)
    proposal_patch: dict[str, AgentParamValue] = Field(default_factory=dict, max_length=16)
    answer_text: str | None = Field(default=None, max_length=1200)
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def _intent_payload_matches(self) -> "ClarificationResolverOutput":
        if self.intent == "param_question" and not self.answer_text:
            raise ValueError("param_question requires answer_text")
        if self.intent != "param_question" and self.answer_text is not None:
            raise ValueError("answer_text is only valid for param_question")
        if self.intent == "edit_params":
            if self.matched_option_id is None and not self.proposal_patch:
                raise ValueError("edit_params requires an option or proposal_patch")
        elif self.matched_option_id is not None or self.proposal_patch:
            raise ValueError("proposal fields are only valid for edit_params")
        return self


PARAM_DESCRIPTIONS: dict[str, str] = {
    "padj_cutoff": "多重检验校正后的显著性阈值，越小越严格。",
    "log2fc_cutoff": "绝对 log2 fold change 的最低阈值，越大越严格。",
    "min_total_count": "特征在所有样本中的最小总计数过滤阈值。",
    "min_replicates": "每个比较组要求的最少生物学重复数。",
    "vip_cutoff": "PLS/统计模型中 VIP 分数的最低阈值。",
    "max_missing_fraction": "允许的最大缺失值比例，超过后会被过滤或拒绝。",
    "impute_method": "缺失值填补所使用的方法。",
    "fdr_cutoff": "基因-代谢物关联结果允许的最大 FDR。",
}


def stable_option_id(
    label: str,
    proposal_patch: Mapping[str, object] | None = None,
    *,
    namespace: str = "contrast",
) -> str:
    """Return an order-independent ID for a candidate option."""

    payload = {
        "label": str(label).strip(),
        "proposal_patch": dict(proposal_patch or {}),
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()[:16]
    return f"{namespace}-{digest}"


def _field_value_type(annotation: object) -> Literal["string", "integer", "number", "boolean", "unknown"]:
    origin = getattr(annotation, "__origin__", None)
    args = get_args(annotation)
    values = args if origin is not None and args else (annotation,)
    if bool in values:
        return "boolean"
    if int in values:
        return "integer"
    if float in values:
        return "number"
    if str in values:
        return "string"
    return "unknown"


def _field_constraint(field: object, name: str) -> float | int | None:
    for metadata in getattr(field, "metadata", ()) or ():
        value = getattr(metadata, name, None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return None


def build_param_spec(
    model: type[BaseModel],
    descriptions: Mapping[str, str] | None = None,
) -> dict[str, ParamFieldSpec]:
    """Build editable parameter specs from ``model.model_fields``.

    ``descriptions`` supplies prose only.  Defaults and numeric constraints
    always come from the Pydantic model, so the resolver cannot drift from the
    execution contract.
    """

    descriptions = descriptions or PARAM_DESCRIPTIONS
    specs: dict[str, ParamFieldSpec] = {}
    for name, field in model.model_fields.items():
        if name in {"analysis_type", "contrast"} or name not in descriptions:
            continue
        default = field.default
        if default is not None and default.__class__.__name__ == "PydanticUndefinedType":
            default = None
        specs[name] = ParamFieldSpec(
            field=name,
            value_type=_field_value_type(field.annotation),
            default=default if isinstance(default, (str, int, float, bool)) or default is None else None,
            ge=_field_constraint(field, "ge"),
            le=_field_constraint(field, "le"),
            description=str(descriptions[name]),
        )
    return specs


def param_specs_for_analysis(analysis_type: str | None) -> dict[str, ParamFieldSpec]:
    """Return only the editable fields for one analysis type."""

    models: dict[str, type[BaseModel]] = {
        "DEG": DEGParams,
        "DEM": DEMParams,
        "GMA": GMAParams,
    }
    model = models.get(str(analysis_type or "").upper())
    return build_param_spec(model) if model is not None else {}


def _normalized(text: str) -> str:
    return re.sub(r"[\s_\-]+", "", text.casefold())


def _param_aliases(field: str) -> tuple[str, ...]:
    aliases = {
        "min_replicates": ("min replicates", "replicates", "重复数", "生物学重复"),
        "log2fc_cutoff": ("log2fc", "log2 fold change", "log2 fold-change", "log2fc cutoff"),
        "padj_cutoff": ("padj", "adjusted p", "adjusted p-value", "校正p", "显著性阈值"),
        "min_total_count": ("min total count", "total count", "总计数"),
        "max_missing_fraction": ("max missing", "missing fraction", "缺失比例"),
        "impute_method": ("impute", "填补方法"),
        "vip_cutoff": ("vip", "vip cutoff"),
        "fdr_cutoff": ("fdr", "fdr cutoff"),
    }
    return (field, field.replace("_", " "), *aliases.get(field, ()))


def _extract_patch(text: str, specs: Mapping[str, ParamFieldSpec]) -> dict[str, AgentParamValue]:
    patch: dict[str, AgentParamValue] = {}
    for field, spec in specs.items():
        aliases = sorted(_param_aliases(field), key=len, reverse=True)
        alias_pattern = "|".join(re.escape(alias) for alias in aliases)
        match = re.search(
            rf"(?:{alias_pattern})\s*(?:改成|改为|设置为|设为|调整为|放宽到|提高到|降低到|=|:|is|to)\s*(-?\d+(?:\.\d+)?|true|false|是|否|[A-Za-z][\w-]*)",
            text,
            flags=re.IGNORECASE,
        )
        if match is None:
            continue
        raw = match.group(1)
        if spec.value_type == "boolean":
            value: AgentParamValue = raw.casefold() in {"true", "是"}
        elif spec.value_type == "integer":
            try:
                value = int(float(raw))
            except ValueError:
                continue
        elif spec.value_type == "number":
            try:
                value = float(raw)
            except ValueError:
                continue
        else:
            value = raw
        patch[field] = value
    return patch


def _ordinal_index(text: str, count: int) -> int | None:
    patterns = (
        r"第\s*(\d+)\s*(?:种|个|项)?",
        r"(?:option|choice)\s*(\d+)",
        r"\b(\d+)(?:st|nd|rd|th)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            index = int(match.group(1)) - 1
            return index if 0 <= index < count else None
    lowered = text.casefold()
    if any(token in lowered for token in ("第二种", "第二个", "后一个", "后者", "the latter", "second")):
        return 1 if count > 1 else None
    if any(token in lowered for token in ("第一种", "第一个", "前一个", "前者", "the former", "first")):
        return 0
    return None


class ClarificationResolver:
    """Classify a pending reply, optionally using an injected model."""

    def __init__(self, model: Callable[[ClarificationResolverInput], object] | object | None = None) -> None:
        self.model = model
        self.last_usage = None

    def __call__(self, request: ClarificationResolverInput) -> ClarificationResolverOutput:
        self.last_usage = None
        if self.model is not None:
            method = getattr(self.model, "resolve_clarification", None)
            if not callable(method):
                method = getattr(self.model, "resolve", None)
            if callable(method):
                try:
                    result = ClarificationResolverOutput.model_validate(method(request))
                    self.last_usage = getattr(self.model, "last_usage", None)
                    return result
                except Exception:
                    # A resolver failure must never make an unvalidated patch
                    # executable.  Fall back to conservative local handling.
                    self.last_usage = getattr(self.model, "last_usage", None)
                    pass
            elif callable(self.model):
                try:
                    result = ClarificationResolverOutput.model_validate(self.model(request))
                    self.last_usage = getattr(self.model, "last_usage", None)
                    return result
                except Exception:
                    self.last_usage = getattr(self.model, "last_usage", None)
                    pass
        return self._deterministic(request)

    def resolve(self, request: ClarificationResolverInput) -> ClarificationResolverOutput:
        """Named alias for callers that prefer an explicit resolver method."""

        return self(request)

    def _deterministic(self, request: ClarificationResolverInput) -> ClarificationResolverOutput:
        text = request.user_reply.strip()
        lowered = text.casefold()
        if any(token in lowered for token in ("cancel", "stop", "discard", "放弃", "取消", "算了", "不用了")):
            return ClarificationResolverOutput(
                intent="cancel", confidence=0.99, reason="用户明确取消当前分析。"
            )
        if any(token in lowered for token in ("what is", "what does", "meaning", "explain", "什么意思", "什么是", "含义", "解释")):
            for field, spec in request.param_spec.items():
                if any(_normalized(alias) in _normalized(text) for alias in _param_aliases(field)):
                    bounds = []
                    if spec.ge is not None:
                        bounds.append(f">={spec.ge:g}")
                    if spec.le is not None:
                        bounds.append(f"<={spec.le:g}")
                    suffix = f"（范围 {'，'.join(bounds)}）" if bounds else ""
                    return ClarificationResolverOutput(
                        intent="param_question",
                        answer_text=f"{field}：{spec.description}{suffix}",
                        confidence=0.98,
                        reason=f"识别到用户在询问参数 {field} 的含义。",
                    )
        patch = _extract_patch(text, request.param_spec)
        if patch:
            return ClarificationResolverOutput(
                intent="edit_params", proposal_patch=patch, confidence=0.94,
                reason="识别到一个或多个阈值参数修改。",
            )
        index = _ordinal_index(text, len(request.options))
        if index is not None:
            option = request.options[index]
            patch = {
                str(key): value
                for key, value in option.proposal_patch.items()
                if isinstance(value, (str, int, float, bool)) or value is None
            }
            return ClarificationResolverOutput(
                intent="edit_params",
                matched_option_id=option.option_id,
                proposal_patch=patch,
                confidence=0.97,
                reason="用户按候选项序号选择了一个 contrast。",
            )
        normalized_text = _normalized(text)
        for option in request.options:
            if _normalized(option.label) in normalized_text:
                return ClarificationResolverOutput(
                    intent="edit_params",
                    matched_option_id=option.option_id,
                    proposal_patch={
                        str(key): value
                        for key, value in option.proposal_patch.items()
                        if isinstance(value, (str, int, float, bool)) or value is None
                    },
                    confidence=0.95,
                    reason="用户复述了候选 contrast。",
                )
        if any(token in lowered for token in ("yes", "confirm", "proceed", "run", "submit", "continue", "确认", "继续", "执行", "提交", "好的", "可以", "就这样")):
            return ClarificationResolverOutput(
                intent="confirm", confidence=0.9, reason="用户确认沿用当前分析参数。"
            )
        return ClarificationResolverOutput(
            intent="unrelated", confidence=0.65, reason="回复未能可靠映射到当前分析澄清。"
        )


__all__ = [
    "ClarificationIntent",
    "ClarificationOption",
    "ClarificationResolver",
    "ClarificationResolverInput",
    "ClarificationResolverOutput",
    "PARAM_DESCRIPTIONS",
    "ParamFieldSpec",
    "build_param_spec",
    "param_specs_for_analysis",
    "stable_option_id",
]
