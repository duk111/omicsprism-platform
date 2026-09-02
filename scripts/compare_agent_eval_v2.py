"""Compare two persisted Eval v2 JSON reports."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.agent.eval_v2 import (
    AgentEvalV2Report,
    compare_agent_eval_reports,
)


def _load(path: Path) -> AgentEvalV2Report:
    return AgentEvalV2Report.model_validate_json(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare two OmicsPrism Eval v2 reports without manual spreadsheets."
    )
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        diff = compare_agent_eval_reports(_load(args.baseline), _load(args.candidate))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    rendered = json.dumps(diff.model_dump(mode="json"), ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
