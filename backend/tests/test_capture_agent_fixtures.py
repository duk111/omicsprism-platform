from __future__ import annotations

from scripts.capture_agent_fixtures import capture


def test_capture_writes_lf_terminated_csv(tmp_path) -> None:
    source = tmp_path / "source.csv"
    target = tmp_path / "fixture.csv"
    source.write_bytes(b"comparison,up_count\r\nsalt_vs_control,3\r\n")

    metadata = capture(source, target, max_rows=20)

    assert target.read_bytes() == b"comparison,up_count\nsalt_vs_control,3\n"
    assert metadata["fixture_rows"] == 1
