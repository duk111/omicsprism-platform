from __future__ import annotations

import io

from fastapi import UploadFile

from backend.app.models import AnalysisType
from backend.app.preflight import PreflightService


class SeekReadOnlyStream:
    def __init__(self, payload: bytes) -> None:
        self._stream = io.BytesIO(payload)

    def seek(self, offset: int) -> int:
        return self._stream.seek(offset)

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)


def test_preflight_accepts_upload_stream_without_readable_method() -> None:
    counts = UploadFile(
        filename="counts.csv",
        file=SeekReadOnlyStream(b"gene,s1,s2\ng1,10,12\n"),
    )
    metadata = UploadFile(
        filename="metadata.csv",
        file=SeekReadOnlyStream(
            b"sample_id,treatment\ns1,control\ns2,salt\n"
        ),
    )

    result = PreflightService().preflight(
        AnalysisType.DIFFERENTIAL,
        params={
            "compare_field": "treatment",
            "tested_levels": "salt",
            "reference_level": "control",
        },
        files={"counts": counts, "metadata": metadata},
    )

    assert result.files[0].rows == 1
    assert result.files[1].rows == 2
    assert not any(issue.code.value == "invalid_csv" for issue in result.errors)
