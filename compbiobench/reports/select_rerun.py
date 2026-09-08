#!/usr/bin/env python3
"""Select CompBioBench questions that still need a rerun.

A question is selected when fewer than 3 models share the same completed
answer, or when it is one of the five default-profile skips (contaminated-rna
q1–q3, encode-atac-pipeline-q1, find-deletion-q1), even if 3+ models already
agree. Timeouts, loop aborts, skipped runs, and ERROR: answers do not count
as completed.

    python compbiobench/reports/select_rerun.py
    python run_benchmark.py run --llm wisp -m "$WISP_MODEL" -i benchmark.csv \
      --resume RUN --profile full --rerun-file reports/rerun_questions.txt
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

from build_index import RESULTS, display_answer, is_loop_abort, is_timeout

HERE = Path(__file__).resolve().parent
MIN_AGREE = 3
# Same membership as FULL_ONLY_QUESTIONS in run_benchmark.py: skipped by --profile default.
DEFAULT_SKIPPED = (
    "contaminated-rna-q1",
    "contaminated-rna-q2",
    "contaminated-rna-q3",
    "encode-atac-pipeline-q1",
    "find-deletion-q1",
)
TXT_NAME = "rerun_questions.txt"
TSV_NAME = "rerun_questions.tsv"


def is_missing(result: dict) -> bool:
    if result.get("status") == "skipped":
        return True
    if is_timeout(result) or is_loop_abort(result):
        return True
    answer = (result.get("answer") or "").strip()
    return answer.lower().startswith("error:")


def classify(max_agree: int) -> str:
    if max_agree <= 0:
        return "no-completed-answer"
    if max_agree == 1:
        return "no-shared-answer"
    return "agreement-below-3"


def is_default_skipped(question_id: str) -> bool:
    return question_id in DEFAULT_SKIPPED


def select_rerun_questions(data: dict, min_agree: int = MIN_AGREE) -> list[dict]:
    models = [model["id"] for model in data["models"]]
    selected = []
    for question in data["questions"]:
        counts: Counter[str] = Counter()
        missing = []
        for model in models:
            result = question["results"][model]
            if is_missing(result):
                missing.append(model)
            else:
                counts[display_answer(result)] += 1
        max_agree = max(counts.values()) if counts else 0
        weak = max_agree < min_agree
        default_skip = is_default_skipped(question["id"])
        if not weak and not default_skip:
            continue
        selected.append({
            "question_id": question["id"],
            "max_agree": max_agree,
            "reason": classify(max_agree) if weak else "default-skipped",
            "missing_models": ",".join(missing),
            "answer_counts": "; ".join(
                f"{count}×{answer.replace(chr(10), ' ')[:80]}"
                for answer, count in counts.most_common()
            ),
        })
    return selected


def write_outputs(rows: list[dict], directory: Path) -> tuple[Path, Path]:
    txt = directory / TXT_NAME
    tsv = directory / TSV_NAME
    txt.write_text("".join(f"{row['question_id']}\n" for row in rows), encoding="utf-8")
    with tsv.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["question_id", "max_agree", "reason", "missing_models", "answer_counts"],
            delimiter="\t",
        )
        writer.writeheader()
        writer.writerows(rows)
    return txt, tsv


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=RESULTS)
    parser.add_argument("--min-agree", type=int, default=MIN_AGREE)
    parser.add_argument("--out-dir", type=Path, default=HERE)
    args = parser.parse_args()
    data = json.loads(args.results.read_text(encoding="utf-8"))
    rows = select_rerun_questions(data, min_agree=args.min_agree)
    txt, tsv = write_outputs(rows, args.out_dir)
    print(f"{len(rows)} / {len(data['questions'])} questions need a rerun")
    print(f"wrote {txt}")
    print(f"wrote {tsv}")
    for row in rows:
        print(f"  {row['question_id']}\t{row['reason']}\tmax_agree={row['max_agree']}")


if __name__ == "__main__":
    main()
