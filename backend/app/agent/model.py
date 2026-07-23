from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Mapping, Protocol, TypeAlias

from pydantic import ValidationError

from .schemas import AgentDecision, ModelContext


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
ModelCompletion: TypeAlias = Callable[[Mapping[str, JsonValue]], Mapping[str, Any] | str]
ModelRepair: TypeAlias = Callable[
    [Mapping[str, JsonValue], Mapping[str, Any] | str],
    Mapping[str, Any] | str,
]


class ModelBoundaryError(ValueError):
    """模型输入或输出违反受控契约。"""


class ModelUnavailableError(RuntimeError):
    """模型功能未配置或模型服务不可用。"""


class ModelAdapter(Protocol):
    """模型适配接口；不校验、不授权，也不持有业务句柄。"""

    def decide(self, context: ModelContext) -> AgentDecision:
        ...


class StructuredModelAdapter:
    """
    受控模型边界：仅将可 JSON 序列化的最小上下文交给完成函数，
    并将返回值校验为 AgentDecision。该类不持有数据库、工具或执行器。
    """

    def __init__(self, complete: ModelCompletion, repair: ModelRepair | None = None) -> None:
        self._complete = complete
        self._repair = repair

    def decide(self, context: ModelContext) -> AgentDecision:
        safe_context = _validate_model_context(context)
        response = self._complete(safe_context)
        try:
            return _validate_model_response(response)
        except ModelBoundaryError:
            if self._repair is None:
                raise
        repaired_response = self._repair(safe_context, response)
        return _validate_model_response(repaired_response)


class UnavailableModelAdapter:
    """未配置模型时使用的显式失败适配器，不生成伪造决策。"""

    def decide(self, context: ModelContext) -> AgentDecision:
        _validate_model_context(context)
        raise ModelUnavailableError("Agent model is not configured")


def _validate_model_context(context: ModelContext) -> dict[str, JsonValue]:
    if not isinstance(context, ModelContext):
        raise ModelBoundaryError("模型上下文必须是 ModelContext")
    return context.model_dump(mode="json")


def _parse_model_response(response: Mapping[str, Any] | str) -> Mapping[str, Any]:
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except json.JSONDecodeError as exc:
            raise ModelBoundaryError("模型返回不是有效 JSON") from exc
    if not isinstance(response, Mapping):
        raise ModelBoundaryError("模型返回必须是 JSON 对象")
    return response


def _validate_model_response(response: Mapping[str, Any] | str) -> AgentDecision:
    payload = _parse_model_response(response)
    try:
        return AgentDecision.model_validate(payload)
    except ValidationError as exc:
        raise ModelBoundaryError("模型返回不符合 AgentDecision 契约") from exc
