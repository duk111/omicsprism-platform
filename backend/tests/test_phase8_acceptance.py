from __future__ import annotations

from scripts.run_phase8_acceptance import _capacity_probe, _percentile


def test_capacity_probe_drains_all_fixed_work_items() -> None:
    report = _capacity_probe(concurrency=4, items=32)

    assert report["concurrency"] == 4
    assert report["items"] == 32
    assert report["enqueued"] == 32
    assert report["pending_after_drain"] == 0
    assert report["processing_after_drain"] == 0
    assert report["reserve_p95_ms"] >= 0


def test_percentile_is_bounded_and_empty_safe() -> None:
    assert _percentile([], 0.95) == 0
    assert _percentile([4.0], 0.95) == 4.0
    assert 1.0 <= _percentile([1.0, 2.0, 3.0, 4.0], 0.95) <= 4.0
