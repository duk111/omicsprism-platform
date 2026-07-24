from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.app.agent.eval import EvalAssemblyFactory, EvalRunner, compare_reports, load_golden_cases
from backend.app.agent.schemas import EvalAssemblyName, EvalRunReport


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

    if args.baseline:
        baseline = EvalRunReport.model_validate_json(args.baseline.read_text(encoding="utf-8"))
        diff = compare_reports(baseline, report)
        diff_path = args.diff_output or args.output.with_name(args.output.stem + ".diff.json")
        diff_path.write_text(diff.model_dump_json(indent=2), encoding="utf-8")

    return 1 if report.summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
