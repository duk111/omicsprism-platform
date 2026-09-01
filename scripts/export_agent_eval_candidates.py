"""Export pending user-feedback candidates without tenant or source identifiers."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.agent.feedback import export_eval_candidate
from backend.app.agent.product_store import PostgresAgentProductStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Export pending Agent feedback for human review. This command never "
            "approves candidates or writes golden evaluation fixtures."
        )
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("OMICS_PRISM_RUNTIME_DATABASE_URL"),
        help="runtime PostgreSQL URL; defaults to OMICS_PRISM_RUNTIME_DATABASE_URL",
    )
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--output", type=Path, help="optional JSON file; default is stdout")
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("--database-url or OMICS_PRISM_RUNTIME_DATABASE_URL is required")
    if not 1 <= args.limit <= 1000:
        parser.error("--limit must be between 1 and 1000")

    store = PostgresAgentProductStore(args.database_url)
    exported = [
        export_eval_candidate(candidate).model_dump(mode="json")
        for candidate in store.list_eval_candidates_for_review(limit=args.limit)
    ]
    payload = json.dumps(exported, ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        print(payload, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
