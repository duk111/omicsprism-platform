from __future__ import annotations

from backend.app.agent.context import build_input_summaries
from backend.app.agent.tools import AgentInputFile, _inspect_input


def _counts_file(row_count: int = 20_000, column_count: int = 3) -> AgentInputFile:
    headers = ["gene"] + [f"sample_{index}" for index in range(column_count - 1)]
    rows = [
        ",".join([f"AT1G{index:05d}"] + ["1"] * (column_count - 1))
        for index in range(row_count)
    ]
    return AgentInputFile("counts.csv", (",".join(headers) + "\n" + "\n".join(rows) + "\n").encode())


def test_large_counts_exposes_bounded_deterministic_feature_sample() -> None:
    inspected = _inspect_input("counts", _counts_file())

    assert len(inspected["feature_id_sample"]) == 15
    assert inspected["feature_id_total"] == 20_000
    assert inspected["feature_id_sample"] == [
        "AT1G00000",
        "AT1G00001",
        "AT1G00002",
        "AT1G00003",
        "AT1G00004",
        "AT1G00005",
        "AT1G00006",
        "AT1G00007",
        "AT1G00008",
        "AT1G00009",
        "AT1G05000",
        "AT1G10000",
        "AT1G15000",
        "AT1G17500",
        "AT1G19999",
    ]


def test_feature_sampling_is_deterministic() -> None:
    item = _counts_file(row_count=100)

    first = _inspect_input("counts", item)
    second = _inspect_input("counts", item)

    assert first["feature_id_sample"] == second["feature_id_sample"]


def test_summary_bounds_columns_and_serialized_size() -> None:
    counts = _inspect_input(
        "counts",
        AgentInputFile(
            "wide_counts.csv",
            (
                ",".join(["gene"] + [f"sample_{index}" for index in range(199)])
                + "\nAT1G00001,"
                + ",".join(["1"] * 199)
                + "\n"
            ).encode(),
        ),
    )
    metadata = _inspect_input(
        "metadata",
        AgentInputFile(
            "metadata.csv",
            (
                "sample_id,treatment,batch\n"
                + "\n".join(f"s{index},salt,b1" for index in range(60))
                + "\n"
            ).encode(),
        ),
    )

    summaries = build_input_summaries([counts, metadata])
    counts_summary, metadata_summary = summaries

    assert counts["column_count"] == 200
    assert len(counts["columns"]) == 12
    assert counts_summary.column_count == 200
    assert len(counts_summary.columns) == 12
    assert len(counts_summary.model_dump_json().encode("utf-8")) < 4 * 1024
    assert len(metadata_summary.model_dump_json().encode("utf-8")) < 4 * 1024
    assert metadata_summary.raw_rows is not None
    assert len(metadata_summary.raw_rows) == 60
    assert all(len(row) == 3 for row in metadata_summary.raw_rows)


def test_metadata_raw_rows_are_omitted_when_row_limit_is_exceeded() -> None:
    content = (
        "sample_id,treatment\n"
        + "\n".join(f"s{index},salt" for index in range(100))
        + "\n"
    ).encode()

    inspected = _inspect_input("metadata", AgentInputFile("metadata.csv", content))
    summary = build_input_summaries([inspected])[0]

    assert inspected["group_replicates"]["treatment"]["salt"] == 100
    assert "raw_rows" not in inspected
    assert summary.raw_rows is None
    assert summary.group_levels
