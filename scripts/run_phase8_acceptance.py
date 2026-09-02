"""Run deterministic Phase 8 fault drills and a bounded queue capacity probe."""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import perf_counter

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.agent.queue import AgentTurnInput, AgentTurnWorkItem, InMemoryAgentTurnQueue


ROOT = Path(__file__).resolve().parents[1]
FAULT_DRILLS: tuple[tuple[str, str], ...] = (
    (
        "runtime_restart_recovery",
        "backend/tests/test_agent_runtime.py::test_runtime_retries_after_process_crash_from_same_checkpoint",
    ),
    (
        "redis_postgres_transient_failure",
        "backend/tests/test_agent_runtime.py::test_runtime_requeues_once_after_transient_database_failure",
    ),
    (
        "duplicate_job_completion",
        "backend/tests/test_agent_reconciliation.py::test_reconciler_duplicate_delivery_reuses_existing_turn",
    ),
)


def _run_drills(*, basetemp: Path) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for name, selector in FAULT_DRILLS:
        started = perf_counter()
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", selector, "-q", f"--basetemp={basetemp / name}"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        results.append({
            "name": name,
            "passed": completed.returncode == 0,
            "duration_ms": round((perf_counter() - started) * 1000, 3),
            "exit_code": completed.returncode,
            "output_tail": "\n".join(completed.stdout.splitlines()[-3:]),
            "error_tail": "\n".join(completed.stderr.splitlines()[-3:]),
        })
    return results


def _capacity_probe(*, concurrency: int, items: int) -> dict[str, object]:
    queue = InMemoryAgentTurnQueue()
    started = perf_counter()

    def enqueue(index: int) -> None:
        queue.enqueue(AgentTurnWorkItem(
            turn_id=f"turn-capacity-{index}",
            thread_id=f"thread-capacity-{index % concurrency}",
            user_id="capacity-user",
            input=AgentTurnInput(message=f"capacity probe {index}"),
        ))

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(enqueue, range(items)))
    enqueue_ms = (perf_counter() - started) * 1000

    reserve_latencies: list[float] = []
    while True:
        reserve_started = perf_counter()
        raw = queue.reserve()
        if raw is None:
            break
        reserve_latencies.append((perf_counter() - reserve_started) * 1000)
        queue.ack(raw)
    return {
        "concurrency": concurrency,
        "items": items,
        "enqueued": len(queue.processing) + len(queue.pending) + len(reserve_latencies),
        "enqueue_ms": round(enqueue_ms, 3),
        "reserve_p95_ms": round(_percentile(reserve_latencies, 0.95), 3),
        "pending_after_drain": len(queue.pending),
        "processing_after_drain": len(queue.processing),
    }


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[max(0, int(percentile * 100) - 1)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Phase 8 fault and capacity acceptance checks")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a short summary")
    parser.add_argument("--output", type=Path, help="write the JSON report to this path")
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--items", type=int, default=128)
    parser.add_argument("--basetemp", type=Path, default=ROOT / ".test-tmp" / "phase-8-3-drills")
    args = parser.parse_args(argv)
    if not 1 <= args.concurrency <= 64 or not 1 <= args.items <= 5000:
        parser.error("--concurrency must be 1-64 and --items must be 1-5000")

    drills = _run_drills(basetemp=args.basetemp)
    report = {
        "phase": "8.3",
        "runner": "deterministic-local",
        "fault_drills": drills,
        "capacity": _capacity_probe(concurrency=args.concurrency, items=args.items),
        "passed": all(bool(item["passed"]) for item in drills),
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    if args.json or not args.output:
        print(rendered)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
