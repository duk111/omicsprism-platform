"""Validate result page locators against owned Job metadata without reading files."""
from fastapi import HTTPException

from .schemas import ResultViewReference


def validate_result_view(job_store, view: ResultViewReference | None,
                         focus_job_ids: list[str], user_id: str) -> ResultViewReference | None:
    if view is None:
        return None
    if focus_job_ids != [view.job_id]:
        raise HTTPException(422, "Result view must match the single focused job")
    try:
        job = job_store.get_for_user(view.job_id, user_id)
    except (KeyError, LookupError) as exc:
        raise HTTPException(404, "Result context unavailable") from exc
    if job.analysis_type.value.upper() != view.analysis_type:
        raise HTTPException(422, "Result view analysis type does not match job")
    if view.artifact is not None:
        # Exact registered reference only: never resolve a client path on disk.
        if not any(item.path == view.artifact for item in [*job.artifacts, *job.result_files]):
            raise HTTPException(404, "Result context unavailable")
    return view


def view_allows_query(state, job_id: str, artifact: str) -> bool:
    """A selected table bounds evidence; selecting overview permits broader queries."""
    view = state.result_view
    return (view is None or view.result_type != "table" or view.artifact is None
            or (view.job_id == job_id and view.artifact == artifact))
