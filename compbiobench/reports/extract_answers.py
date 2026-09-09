#!/usr/bin/env python3
"""Extract answers from local benchmark_runs question result.json files.

Does not filter by profile. One column per model (latest run if a model
appears twice).

    python reports/extract_answers.py
    python reports/extract_answers.py --runs-dir benchmark_runs
    python reports/extract_answers.py --wide -o answers_wide.csv
    python reports/extract_answers.py --long -o answers_long.tsv
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_RUNS = HERE.parent / "benchmark_runs"


def load_cells(runs_dir: Path) -> tuple[list[str], dict[str, dict[str, dict]]]:
    """Return (model_ids in run order, cells[qid][model] = record)."""
    runs = []
    for path in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
        meta_path = path / "run_metadata.json"
        if not meta_path.is_file():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        runs.append((path, meta))
    models: list[str] = []
    seen: dict[str, Path] = {}
    cells: dict[str, dict[str, dict]] = {}
    for path, meta in runs:
        model = str(meta.get("model") or path.name)
        if model in seen:
            print(f"warning: {model} also in {seen[model].name}; using {path.name}")
        seen[model] = path
        if model not in models:
            models.append(model)
        questions = path / "questions"
        if not questions.is_dir():
            continue
        for result_path in sorted(questions.glob("*/result.json")):
            data = json.loads(result_path.read_text(encoding="utf-8"))
            qid = data.get("question_id") or result_path.parent.name
            answer = (data.get("output") or {}).get("answer")
            if not isinstance(answer, str):
                answer = "" if answer is None else str(answer)
            cells.setdefault(qid, {})[model] = {
                "run": path.name,
                "status": data.get("status") or "",
                "elapsed": (data.get("execution") or {}).get("elapsed_time"),
                "answer": answer,
            }
    # Drop models that were superseded so column order follows first appearance,
    # but cells only keep the last run per model (loop already overwrote).
    models = list(seen)
    return models, cells


def write_wide(path: Path, models: list[str], cells: dict[str, dict[str, dict]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["question_id", *models])
        for qid in sorted(cells):
            writer.writerow([qid] + [cells[qid].get(m, {}).get("answer", "") for m in models])


def write_long(path: Path, models: list[str], cells: dict[str, dict[str, dict]]) -> None:
    delim = "\t" if path.suffix.lower() in {".tsv", ".tab"} else ","
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, delimiter=delim, lineterminator="\n")
        writer.writerow(["question_id", "model", "run", "status", "elapsed_seconds", "answer"])
        for qid in sorted(cells):
            for model in models:
                rec = cells[qid].get(model)
                if not rec:
                    continue
                writer.writerow([
                    qid, model, rec["run"], rec["status"], rec["elapsed"], rec["answer"],
                ])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS)
    parser.add_argument("--wide", type=Path, nargs="?", const=Path("answers_wide.csv"),
                        help="write question × model CSV (default name if flag has no path)")
    parser.add_argument("--long", type=Path, nargs="?", const=Path("answers_long.tsv"),
                        help="write one-row-per-cell TSV/CSV")
    parser.add_argument("-o", "--output", type=Path, default=None,
                        help="shorthand for --wide PATH")
    args = parser.parse_args()
    runs_dir = args.runs_dir.expanduser()
    if not runs_dir.is_dir():
        raise SystemExit(f"no runs directory: {runs_dir}")
    models, cells = load_cells(runs_dir)
    print(f"{len(cells)} questions × {len(models)} models from {runs_dir}")
    for model in models:
        n = sum(model in cells[qid] for qid in cells)
        print(f"  {model}: {n} results")
    wide = args.output or args.wide
    if wide is None and args.long is None:
        wide = Path("answers_wide.csv")
    if wide is not None:
        write_wide(wide, models, cells)
        print(f"wrote {wide}")
    if args.long is not None:
        write_long(args.long, models, cells)
        print(f"wrote {args.long}")


if __name__ == "__main__":
    main()
