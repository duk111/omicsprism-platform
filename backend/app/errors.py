from __future__ import annotations

from enum import Enum
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field


class ErrorCategory(str, Enum):
    INPUT = "input_error"
    PERMISSION = "permission_error"
    RESOURCE = "resource_error"
    ANALYSIS = "analysis_failed"
    SYSTEM = "system_error"


class ApiErrorDetail(BaseModel):
    category: ErrorCategory
    code: str
    message: str
    user_message: str
    suggestions: list[str] = Field(default_factory=list)
    technical_detail: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class AppException(HTTPException):
    def __init__(
        self,
        status_code: int,
        *,
        category: ErrorCategory,
        code: str,
        user_message: str,
        message: str | None = None,
        suggestions: list[str] | None = None,
        technical_detail: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        detail = ApiErrorDetail(
            category=category,
            code=code,
            message=message or user_message,
            user_message=user_message,
            suggestions=suggestions or [],
            technical_detail=technical_detail,
            context=context or {},
        )
        super().__init__(status_code=status_code, detail=detail.model_dump(mode="json"))


def input_error(code: str, user_message: str, **kwargs: Any) -> AppException:
    return AppException(400, category=ErrorCategory.INPUT, code=code, user_message=user_message, **kwargs)


def permission_error(code: str, user_message: str, **kwargs: Any) -> AppException:
    return AppException(403, category=ErrorCategory.PERMISSION, code=code, user_message=user_message, **kwargs)


def resource_error(code: str, user_message: str, **kwargs: Any) -> AppException:
    return AppException(404, category=ErrorCategory.RESOURCE, code=code, user_message=user_message, **kwargs)


def analysis_error(code: str, user_message: str, **kwargs: Any) -> AppException:
    return AppException(422, category=ErrorCategory.ANALYSIS, code=code, user_message=user_message, **kwargs)


def category_for_status(status_code: int) -> ErrorCategory:
    if status_code in {400, 409, 413, 422}:
        return ErrorCategory.INPUT
    if status_code in {401, 403}:
        return ErrorCategory.PERMISSION
    if status_code == 404:
        return ErrorCategory.RESOURCE
    return ErrorCategory.SYSTEM


def default_user_message(category: ErrorCategory) -> str:
    if category == ErrorCategory.INPUT:
        return "The submitted data or parameters could not be accepted."
    if category == ErrorCategory.PERMISSION:
        return "You do not have access to this resource. Please sign in again or contact an administrator."
    if category == ErrorCategory.RESOURCE:
        return "The requested job or file could not be found."
    if category == ErrorCategory.ANALYSIS:
        return "The analysis could not be completed."
    return "The server could not complete the request. Please try again later."


def suggestions_for_category(category: ErrorCategory) -> list[str]:
    if category == ErrorCategory.INPUT:
        return [
            "Check that required CSV files were uploaded.",
            "Confirm sample names and group columns match the selected analysis type.",
        ]
    if category == ErrorCategory.PERMISSION:
        return ["Sign in again.", "Ask the job owner or administrator for access."]
    if category == ErrorCategory.RESOURCE:
        return ["Return to the workbench and open the job from your recent jobs list."]
    if category == ErrorCategory.ANALYSIS:
        return analysis_failure_suggestions("")
    return ["Retry the operation.", "If the problem persists, share the job id with an administrator."]


def analysis_failure_suggestions(error_text: str) -> list[str]:
    text = error_text.lower()
    suggestions = [
        "Check that sample names are identical across expression, metabolome, counts, metadata, and group files.",
        "Confirm the selected group or compare column exists and contains the expected levels.",
        "Review the job log for the exact failing step before resubmitting.",
    ]
    if any(term in text for term in ("memory", "killed", "out of memory", "cannot allocate")):
        suggestions.insert(0, "Reduce the input size or filter low-abundance features before submitting.")
    if any(term in text for term in ("sample", "index", "shape", "length", "align")):
        suggestions.insert(0, "Check for sample order mismatches, duplicated sample IDs, or missing samples.")
    if any(term in text for term in ("group", "compare", "level", "metadata", "column")):
        suggestions.insert(0, "Check the metadata/group table and make sure the grouping column and levels are spelled exactly.")
    if any(term in text for term in ("csv", "parse", "could not convert", "nan", "numeric")):
        suggestions.insert(0, "Check CSV formatting and make sure numeric matrices do not contain text values.")
    return list(dict.fromkeys(suggestions))


def analysis_failure_detail(error_text: str | None) -> ApiErrorDetail:
    technical = error_text or "Analysis failed without a detailed message."
    return ApiErrorDetail(
        category=ErrorCategory.ANALYSIS,
        code="analysis_job_failed",
        message=technical,
        user_message="The analysis stopped before results were generated.",
        suggestions=analysis_failure_suggestions(technical),
        technical_detail=technical,
    )

