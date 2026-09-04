#!/usr/bin/env python3
"""Build runner-ready benchmark.csv from the Hugging Face dataset dump.

Resolves each file_paths entry to an absolute path under --data-dir.
Missing files are kept as-is and listed on stderr; pass --strict to fail.

    python compbiobench/prepare_csv.py \\
      --data-dir ~/benchmark/compbiobench-data \\
      --out compbiobench/benchmark.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path


def index_files(root: Path) -> dict[str, Path]:
    index: dict[str, Path] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        index.setdefault(path.name, path)
    return index


def find_questions(data_dir: Path) -> Path:
    for name in ("compbiobench.v1.tsv", "compbiobench.v1.csv", "benchmark.csv"):
        for candidate in (data_dir / name, data_dir / "data" / name):
            if candidate.is_file():
                return candidate
    raise SystemExit(
        f"no question table under {data_dir} (looked for compbiobench.v1.tsv)"
    )


def resolve_paths(raw: str, index: dict[str, Path], data_dir: Path) -> tuple[str, list[str]]:
    missing: list[str] = []
    resolved: list[str] = []
    for part in raw.split(","):
        token = part.strip()
        if not token or token.lower() in {"null", "none", "nan"}:
            continue
        direct = Path(token)
        if direct.is_file():
            resolved.append(str(direct.resolve()))
            continue
        under = data_dir / token
        if under.is_file():
            resolved.append(str(under.resolve()))
            continue
        under_data = data_dir / "data" / token
        if under_data.is_file():
            resolved.append(str(under_data.resolve()))
            continue
        hit = index.get(Path(token).name)
        if hit is not None:
            resolved.append(str(hit.resolve()))
            continue
        missing.append(token)
        resolved.append(token)
    return ",".join(resolved), missing


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent / "benchmark.csv",
    )
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    data_dir = args.data_dir.resolve()
    src = find_questions(data_dir)
    index = index_files(data_dir)
    dialect = csv.excel_tab if src.suffix.lower() == ".tsv" else csv.excel
    missing_all: list[str] = []
    rows: list[dict] = []
    with src.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, dialect=dialect)
        if not reader.fieldnames or "question_id" not in reader.fieldnames:
            raise SystemExit(f"{src} is missing question_id")
        fieldnames = list(reader.fieldnames)
        for row in reader:
            raw = row.get("file_paths") or ""
            rewritten, missing = resolve_paths(raw, index, data_dir)
            row["file_paths"] = rewritten
            missing_all.extend(f"{row.get('question_id')}: {m}" for m in missing)
            rows.append(row)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"wrote {args.out}  ({len(rows)} questions from {src})")
    if missing_all:
        print(f"{len(missing_all)} file_paths not found:", file=sys.stderr)
        for line in missing_all[:30]:
            print(f"  {line}", file=sys.stderr)
        if len(missing_all) > 30:
            print(f"  ... {len(missing_all) - 30} more", file=sys.stderr)
        if args.strict:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
