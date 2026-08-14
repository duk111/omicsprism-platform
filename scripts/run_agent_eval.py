from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.app.agent.eval import EvalAssemblyFactory, EvalRunner, compare_reports, load_golden_cases
from backend.app.agent.schemas import EvalAssemblyName, EvalRunReport

E2E_CASES_PATH = Path(__file__).resolve().parents[1] / "backend" / "tests" / "test_agent_end_to_end.py"


def _e2e_scenario_count() -> int:
    text = E2E_CASES_PATH.read_text(encoding="utf-8")
    return sum(1 for line in text.splitlines() if line.startswith("def test_"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run OmicsPrism agent golden evaluations")
    parser.add_argument("--assembly", choices=[item.value for item in EvalAssemblyName], default="unit")
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--diff-output", type=Path)
    parser.add_argument("--label", default="stub-fixture")
    args = parser.parse_args(argv)

    factory = EvalAssemblyFactory()
    if args.assembly == EvalAssemblyName.UNIT.value:
        assembly = factory.unit(label=args.label)
    elif args.assembly == EvalAssemblyName.OFFLINE.value:
        assembly = factory.offline()
    else:
        assembly = factory.production()

    cases = load_golden_cases(args.cases) if args.cases else load_golden_cases()
    report = EvalRunner().run(cases, assembly)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    print(report.model_dump_json(indent=2))
    # 口径说明：pass_rate 只针对组件级 golden 用例；端到端多轮场景以 pytest 独立运行，
    # 两者不要合并解读（上一轮的问题正是「测试全过但体验差」）。
    print("\n# 口径说明（两个指标相互独立，请勿合并）")
    print(f"# - 组件级 golden 用例：{len(cases)} 条，pass_rate / 各 metric 仅覆盖这一类。")
    print(f"# - 端到端多轮场景：{_e2e_scenario_count()} 条（backend/tests/test_agent_end_to_end.py，以 pytest 运行）。")

    if args.baseline:
        baseline = EvalRunReport.model_validate_json(args.baseline.read_text(encoding="utf-8"))
        diff = compare_reports(baseline, report)
        diff_path = args.diff_output or args.output.with_name(args.output.stem + ".diff.json")
        diff_path.write_text(diff.model_dump_json(indent=2), encoding="utf-8")

    return 1 if report.summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
