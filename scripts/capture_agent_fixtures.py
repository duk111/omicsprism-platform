from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_ARTIFACTS = (
    "differential_gene_counts.csv",
    "T02_High_Confidence_Network.csv",
)


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def capture(source: Path, target: Path, *, max_rows: int) -> dict[str, object]:
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with source.open("r", encoding="utf-8-sig", newline="") as input_handle:
        reader = csv.reader(input_handle)
        with target.open("w", encoding="utf-8", newline="") as output_handle:
            writer = csv.writer(output_handle)
            for index, row in enumerate(reader):
                if index > max_rows:
                    break
                writer.writerow(row)
                if index > 0:
                    written += 1
    return {
        "source_artifact": source.name,
        "source_checksum": _checksum(source),
        "fixture": target.name,
        "fixture_checksum": _checksum(target),
        "fixture_rows": written,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture minimal real OmicsPrism result fixtures")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--runs-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("backend/app/agent/fixtures"))
    parser.add_argument("--max-rows", type=int, default=20)
    parser.add_argument("--artifact", action="append", dest="artifacts")
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    source_dir = (args.runs_dir / args.job_id / "outputs").resolve()
    if not source_dir.is_dir():
        raise SystemExit(f"job output directory not found: {source_dir}")

    manifest: dict[str, object] = {
        "source_job_id": args.job_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "max_rows": args.max_rows,
        "artifacts": [],
    }
    names = tuple(args.artifacts or DEFAULT_ARTIFACTS)
    for name in names:
        matches = list(source_dir.rglob(name))
        if not matches:
            continue
        source = matches[0]
        target = output_dir / name
        manifest["artifacts"].append(capture(source, target, max_rows=args.max_rows))

    if not manifest["artifacts"]:
        raise SystemExit("none of the requested artifacts were found")
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(json.dumps(manifest, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
