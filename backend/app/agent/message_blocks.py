from __future__ import annotations

from urllib.parse import quote

from ..models import JobStatus
from .graph import JobSummary
from .schemas import AgentJobBlock, AgentTextBlock


def text_block(text: str) -> AgentTextBlock:
    """Create the required visible text block for one completed Agent turn."""

    return AgentTextBlock(text=text)


def job_block(
    job_id: str,
    status: JobStatus,
    *,
    progress: int = 0,
) -> AgentJobBlock:
    """Build public navigation data from an ownership-bound Job identifier."""

    encoded_job_id = quote(job_id, safe="")
    return AgentJobBlock(
        job_id=job_id,
        status=status,
        progress=progress,
        progress_url=f"/jobs/{encoded_job_id}",
        results_url=(
            f"/jobs/{encoded_job_id}/results"
            if status is JobStatus.SUCCEEDED
            else None
        ),
    )


def job_block_from_summary(summary: JobSummary) -> AgentJobBlock | None:
    """Project only a known public Job status; unknown status stays textual."""

    try:
        status = JobStatus(summary.status)
    except ValueError:
        return None
    return job_block(
        summary.job_id,
        status,
        progress=summary.progress if summary.progress is not None else 0,
    )
