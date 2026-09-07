#!/usr/bin/env python3
"""Import result.json entries from a run ZIP, then build the answer table.

    python compbiobench/reports/build_index.py --archive archive_clean.zip
    python compbiobench/reports/build_index.py  # rebuild from saved results.json

Only the small result records are read; traces and raw logs stay in the ZIP.
"""

import argparse
import csv
import json
from html import escape
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

HERE = Path(__file__).resolve().parent
OUT = HERE.parents[1] / "docs" / "compbiobench"
RESULTS = HERE / "results.json"


def import_archive(path):
    models, questions = {}, {}
    with ZipFile(path) as archive:
        for name in sorted(archive.namelist()):
            if not name.endswith("/run_metadata.json"):
                continue
            meta = json.loads(archive.read(name))
            model = meta["model"]
            if model in models:
                raise ValueError(f"Multiple runs for {model}; select one run per model")
            run = str(PurePosixPath(name).parent)
            models[model] = {
                "id": model,
                "run": PurePosixPath(run).name,
                "timestamp": meta["timestamp"],
                "timeout_minutes": meta["timeout_minutes"],
                "reasoning_effort": meta.get("model_reasoning_effort"),
                "total_questions": meta["total_questions"],
                "benchmark_profile": meta.get("benchmark_profile"),
                "benchmark_profile_version": meta.get("benchmark_profile_version"),
                "selected_question_ids": meta.get("selected_question_ids"),
                "skipped_questions": meta.get("skipped_questions", {}),
            }
            for entry in sorted(archive.namelist()):
                if not (entry.startswith(run + "/questions/") and entry.endswith("/result.json")):
                    continue
                result = json.loads(archive.read(entry))
                qid = result["question_id"]
                question = questions.setdefault(qid, {
                    "id": qid, "index": result["idx"],
                    "question": result["input"]["question"], "results": {},
                })
                if model in question["results"]:
                    raise ValueError(f"Duplicate result: {model}/{qid}")
                if (question["index"] != result["idx"]
                        or question["question"] != result["input"]["question"]
                        or result["execution"]["model"] != model):
                    raise ValueError(f"Conflicting question or model: {entry}")
                answer = result["output"]["answer"]
                if not isinstance(answer, str):
                    raise ValueError(f"Expected a text answer: {entry}")
                question["results"][model] = {
                    "answer": answer,
                    "status": result["status"],
                    "timestamp": result["execution"]["timestamp"],
                }
    if not models or not questions:
        raise ValueError("No benchmark results found in archive")
    for model, meta in models.items():
        actual = {qid for qid, question in questions.items() if model in question["results"]}
        if len(actual) != meta["total_questions"]:
            raise ValueError(f"Incomplete question set for {model}: {len(actual)} results")
        selected = meta["selected_question_ids"]
        if selected is not None and (set(selected) != actual or len(selected) != len(actual)):
            raise ValueError(f"Selected question IDs do not match results for {model}")
        missing = set(questions) - actual
        if missing != set(meta["skipped_questions"]):
            raise ValueError(f"Missing questions are not explained by run metadata for {model}")
        for qid in missing:
            questions[qid]["results"][model] = {
                "answer": "NA", "status": "skipped", "timestamp": None,
                "reason": meta["skipped_questions"][qid],
            }
    return {
        "source": path.name,
        "models": list(models.values()),
        "questions": sorted(questions.values(), key=lambda q: (q["index"], q["id"])),
    }


def is_timeout(result):
    return result["answer"].strip().lower() == "error: timeout"


def is_loop_abort(result):
    return result["answer"].startswith("ERROR: 检测到智能体连续重复相同的工具调用/结果循环")


def display_answer(result):
    return "NA" if is_timeout(result) or is_loop_abort(result) or result.get("status") == "skipped" else result["answer"]


def render(data):
    headers, rows = [], []
    for model in data["models"]:
        results = [q["results"][model["id"]] for q in data["questions"]]
        timeouts = sum(is_timeout(r) for r in results)
        loops = sum(is_loop_abort(r) for r in results)
        errors = sum(r["status"] == "error" and not is_timeout(r) and not is_loop_abort(r) for r in results)
        skipped = sum(r["status"] == "skipped" for r in results)
        note = f"{len(results) - skipped} 题已运行 · {timeouts} 超时"
        if skipped:
            note += f" · {skipped} 未运行"
        if loops:
            note += f" · {loops} 循环中断"
        if errors:
            note += f" · {errors} 运行错误"
        headers.append(f'<th scope="col">{escape(model["id"])}<small>{note}</small></th>')
    for question in data["questions"]:
        cells = []
        groups = {}
        for model in data["models"]:
            result = question["results"][model["id"]]
            answer = display_answer(result)
            group = 0 if answer == "NA" else groups.setdefault(answer, len(groups) + 1)
            attr = ' class="timeout" title="timeout：超时，暂记 NA"' if is_timeout(result) else ""
            if is_loop_abort(result):
                attr = ' class="loop-abort" title="重复工具调用中断，暂记 NA"'
            if result["status"] == "skipped":
                attr = f' class="skipped" title="未运行：{escape(result["reason"], quote=True)}"'
            cells.append(f'<td{attr} data-answer-group="{group}">{escape(answer)}</td>')
        rows.append(
            f'<tr><th scope="row"><details><summary>{escape(question["id"])}</summary>'
            f'<p>{escape(question["question"])}</p></details></th>{"".join(cells)}</tr>'
        )
    all_results = [r for q in data["questions"] for r in q["results"].values()]
    dates = sorted(r["timestamp"][:10] for r in all_results if r["timestamp"])
    replacements = {
        "HEADERS": "".join(headers), "ROWS": "\n".join(rows),
        "QUESTIONS": str(len(data["questions"])), "MODELS": str(len(data["models"])),
        "TIMEOUTS": str(sum(is_timeout(r) for r in all_results)),
        "SKIPPED": str(sum(r["status"] == "skipped" for r in all_results)),
        "LOOP_ABORTS": str(sum(is_loop_abort(r) for r in all_results)),
        "DATES": escape(f"{dates[0]} – {dates[-1]}"),
        "SOURCE": escape(data["source"]),
    }
    html = (HERE / "index.template.html").read_text(encoding="utf-8")
    for key, value in replacements.items():
        html = html.replace(f"@@{key}@@", value)
    return html


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="import a run ZIP before building")
    args = parser.parse_args()
    if args.archive:
        data = import_archive(args.archive)
        RESULTS.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        data = json.loads(RESULTS.read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "index.html").write_text(render(data), encoding="utf-8")
    with (OUT / "answers.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file, lineterminator="\n")
        writer.writerow(["question_id"] + [m["id"] for m in data["models"]])
        for question in data["questions"]:
            writer.writerow([question["id"]] + [
                display_answer(question["results"][m["id"]]) for m in data["models"]
            ])
    print(f"Built {OUT}: {len(data['questions'])} questions × {len(data['models'])} models")


if __name__ == "__main__":
    main()
