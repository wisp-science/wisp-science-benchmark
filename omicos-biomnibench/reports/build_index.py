#!/usr/bin/env python3
"""Build a self-contained GitHub Pages leaderboard from reports/*/matrix.csv.

Usage:
    python omicos-biomnibench/reports/build_index.py
    python omicos-biomnibench/reports/build_index.py --out docs/index.html

Scans sibling run folders (each with matrix.csv), embeds compact scores into
index.template.html, and writes:

    omicos-biomnibench/reports/index.html   (open locally)
    docs/index.html                         (GitHub Pages, /docs)
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from datetime import date, datetime, timezone
from pathlib import Path

PASS_THRESHOLD = 0.70

# Display names for known run-ids. Unknown ids fall back to stripping "wisp-".
LABELS = {
    "wisp-kimi-k3": "Kimi K3",
    "wisp-grok-4.6": "Grok 4.6",
    "wisp-glm-5.3": "GLM-5.3",
    "wisp-deepseek-flash-vision": "DeepSeek Flash",
    "wisp-deepseek-v4-pro": "DeepSeek v4-pro",
    "wisp-gpt-5.6-sol": "GPT-5.6",
    "wisp-gemini-3.8-flash": "Gemini 3.8 Flash",
}

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
TEMPLATE = HERE / "index.template.html"


def task_sort_key(task_id: str) -> tuple[int, ...]:
    parts = task_id.lower().removeprefix("da-").split("-")
    out = []
    for p in parts:
        try:
            out.append(int(p))
        except ValueError:
            out.append(0)
    return tuple(out)


def label_for(run_id: str) -> str:
    if run_id in LABELS:
        return LABELS[run_id]
    name = run_id[5:] if run_id.startswith("wisp-") else run_id
    return name.replace("-", " ")


def parse_bool(value: str | None) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def parse_float(value: str | None) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_int(value: str | None) -> int | None:
    n = parse_float(value)
    return int(n) if n is not None else None


def load_run(csv_path: Path) -> list[dict]:
    rows: list[dict] = []
    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            score = parse_float(raw.get("score"))
            correct = parse_bool(raw.get("correct"))
            # Prefer the official pass rule when score is present.
            passed = score >= PASS_THRESHOLD if score is not None else correct
            paper = raw.get("paper") or ""
            try:
                paper_n = int(paper)
            except (TypeError, ValueError):
                paper_n = paper
            rows.append(
                {
                    "task": raw.get("task_id") or "",
                    "paper": paper_n,
                    "status": raw.get("status") or "",
                    "pass": passed,
                    "score": score,
                    "elapsed_s": parse_float(raw.get("elapsed_s")),
                    "tool_calls": parse_int(raw.get("tool_calls")),
                    "grade_mode": raw.get("grade_mode") or "",
                    "answer": raw.get("final_answer") or "",
                    "notes": raw.get("grader_notes") or "",
                    "error": raw.get("error") or "",
                }
            )
    rows.sort(key=lambda r: task_sort_key(r["task"]))
    return rows


def summarize(run_id: str, rows: list[dict]) -> dict:
    scores = [r["score"] for r in rows if r["score"] is not None]
    elapsed = [r["elapsed_s"] for r in rows if r["elapsed_s"] is not None]
    tools = [r["tool_calls"] for r in rows if r["tool_calls"] is not None]
    n = len(rows)
    passed = sum(1 for r in rows if r["pass"])
    mean = (sum(scores) / len(scores)) if scores else None
    return {
        "id": run_id,
        "label": label_for(run_id),
        "ran": n,
        "answered": sum(1 for r in rows if r["status"] == "ok"),
        "passed": passed,
        "accuracy": (passed / n) if n else 0.0,
        "mean_score": mean,
        "median_score": statistics.median(scores) if scores else None,
        "mean_elapsed_s": (sum(elapsed) / len(elapsed)) if elapsed else None,
        "median_elapsed_s": statistics.median(elapsed) if elapsed else None,
        "mean_tool_calls": (sum(tools) / len(tools)) if tools else None,
        "median_tool_calls": statistics.median(tools) if tools else None,
        "perfect": sum(1 for s in scores if s >= 0.999),
    }


def collect(reports_dir: Path) -> dict:
    runs: dict[str, list[dict]] = {}
    for child in sorted(reports_dir.iterdir()):
        if not child.is_dir():
            continue
        csv_path = child / "matrix.csv"
        if not csv_path.is_file():
            continue
        rows = load_run(csv_path)
        if not rows:
            print(f"skip empty {csv_path}", file=sys.stderr)
            continue
        runs[child.name] = rows

    if not runs:
        raise SystemExit(f"no matrix.csv runs found under {reports_dir}")

    models = [summarize(run_id, rows) for run_id, rows in runs.items()]
    models.sort(
        key=lambda m: (
            -m["passed"],
            -(m["mean_score"] or 0.0),
            m["label"].lower(),
        )
    )

    task_map: dict[str, int | str] = {}
    for rows in runs.values():
        for row in rows:
            task_map.setdefault(row["task"], row["paper"])
    tasks = [
        {"id": tid, "paper": task_map[tid]}
        for tid in sorted(task_map, key=task_sort_key)
    ]

    cells: dict[str, dict[str, dict]] = {}
    traces: dict[str, dict[str, dict]] = {}
    for run_id, rows in runs.items():
        cells[run_id] = {}
        traces[run_id] = {}
        for row in rows:
            cells[run_id][row["task"]] = {
                "score": row["score"],
                "pass": row["pass"],
                "elapsed_s": row["elapsed_s"],
                "tool_calls": row["tool_calls"],
                "status": row["status"],
            }
            traces[run_id][row["task"]] = {
                "grade_mode": row["grade_mode"],
                "answer": row["answer"],
                "notes": row["notes"],
                "error": row["error"],
            }

    board = {
        "generated_at": date.today().isoformat(),
        "generated_ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "protocol": {
            "agent": "wisp-science",
            "backend": "wisp",
            "judge": "deepseek-v4-pro",
            "pass_threshold": PASS_THRESHOLD,
            "n_tasks": len(tasks),
            "n_models": len(models),
            "dataset": "BiomniBench-DA",
            "dataset_url": "https://huggingface.co/datasets/phylobio/BiomniBench-DA",
            "agent_url": "https://github.com/xuzhougeng/wisp-science",
            "repo_url": "https://github.com/wisp-science/wisp-science-benchmark",
            "protocol_url": "https://github.com/wisp-science/wisp-science-benchmark/blob/main/omicos-biomnibench/README.md",
        },
        "models": models,
        "tasks": tasks,
        "cells": cells,
    }
    return board, traces


def render(data: dict) -> str:
    if not TEMPLATE.is_file():
        raise SystemExit(f"missing template: {TEMPLATE}")
    html = TEMPLATE.read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
    if "@@BENCH_DATA@@" not in html or "@@GENERATED_AT@@" not in html:
        raise SystemExit("template is missing @@BENCH_DATA@@ / @@GENERATED_AT@@ placeholders")
    return html.replace("@@BENCH_DATA@@", payload).replace(
        "@@GENERATED_AT@@", data["generated_at"]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=HERE,
        help="directory that contains <run-id>/matrix.csv folders",
    )
    parser.add_argument(
        "--out",
        type=Path,
        action="append",
        help="output HTML path (repeatable). Defaults: reports/index.html and docs/index.html",
    )
    args = parser.parse_args()
    reports_dir = args.reports_dir.resolve()
    data, traces = collect(reports_dir)
    html = render(data)
    traces_json = json.dumps(traces, ensure_ascii=False, separators=(",", ":"))

    outs = args.out or [HERE / "index.html", REPO_ROOT / "docs" / "index.html"]
    for out in outs:
        out = out.resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        cells_path = out.parent / "cells.json"
        cells_path.write_text(traces_json, encoding="utf-8")
        print(
            f"wrote {out}  ({data['protocol']['n_models']} models, "
            f"{data['protocol']['n_tasks']} tasks)"
        )
        print(f"wrote {cells_path}  ({cells_path.stat().st_size:,} bytes)")

    nojekyll = REPO_ROOT / "docs" / ".nojekyll"
    if (REPO_ROOT / "docs").is_dir():
        nojekyll.touch()


if __name__ == "__main__":
    main()
