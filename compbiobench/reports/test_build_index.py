"""Run with python compbiobench/reports/test_build_index.py (stdlib only)."""

import csv
import json
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from zipfile import ZipFile

from build_index import HERE, OUT, display_answer, import_archive, is_loop_abort, is_timeout, render


class AnswerTable(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cells = []
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "td":
            self.cell = ""

    def handle_data(self, text):
        if self.cell is not None:
            self.cell += text

    def handle_endtag(self, tag):
        if tag == "td":
            self.cells.append(self.cell)
            self.cell = None


def check():
    # Exercise ZIP alignment, exact answer preservation, timeouts, and HTML escaping.
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "runs.zip"
        with ZipFile(path, "w") as archive:
            for model in ("model-b", "model-a"):
                run = f"benchmark_runs/{model}"
                meta = {"model": model, "timestamp": "20260906_120000",
                        "timeout_minutes": 120, "total_questions": 2}
                archive.writestr(f"{run}/run_metadata.json", json.dumps(meta))
                for index, qid in ((1, "first-in-zip"), (0, "second-in-zip")):
                    answer = "ERROR: timeout" if model == "model-b" else "</td><script>&\n  NA  "
                    result = {
                        "idx": index, "question_id": qid, "status": "error" if model == "model-b" else "success",
                        "input": {"question": "Question <&>"},
                        "execution": {"model": model, "timestamp": "2026-09-06T12:00:00"},
                        "output": {"answer": answer},
                    }
                    archive.writestr(f"{run}/questions/{qid}/result.json", json.dumps(result))
        data = import_archive(path)
        assert [q["index"] for q in data["questions"]] == [0, 1]
        assert [m["id"] for m in data["models"]] == ["model-a", "model-b"]
        parsed = AnswerTable()
        html = render(data)
        parsed.feed(html)
        assert parsed.cells == ["</td><script>&\n  NA  ", "NA"] * 2
        assert "Question &lt;&amp;&gt;" in html
        assert "@@" not in html
        assert display_answer({"answer": "ERROR: api disconnected"}) == "ERROR: api disconnected"
        assert not is_timeout({"answer": "NA"})
        loop = {"answer": "ERROR: 检测到智能体连续重复相同的工具调用/结果循环且没有进展，已中断", "status": "error"}
        assert is_loop_abort(loop) and not is_timeout(loop)
        assert display_answer(loop) == "NA"
        original = data["questions"][0]["results"]["model-a"]
        data["questions"][0]["results"]["model-a"] = {**original, **loop}
        assert 'class="loop-abort"' in render(data)
        assert "ERROR: 检测到" not in render(data)
        data["questions"][0]["results"]["model-a"] = original

        # A declared skip must remain distinct from a timeout or lost result file.
        with ZipFile(path) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        meta_name = "benchmark_runs/model-b/run_metadata.json"
        meta = json.loads(entries[meta_name])
        meta.update(total_questions=1, selected_question_ids=["second-in-zip"],
                    skipped_questions={"first-in-zip": 'External reference <bundle> "missing"'})
        entries[meta_name] = json.dumps(meta)
        del entries["benchmark_runs/model-b/questions/first-in-zip/result.json"]
        subset_path = Path(directory) / "subset.zip"
        with ZipFile(subset_path, "w") as archive:
            for name, content in entries.items():
                archive.writestr(name, content)
        subset = import_archive(subset_path)
        skipped = subset["questions"][1]["results"]["model-b"]
        assert skipped["status"] == "skipped" and skipped["timestamp"] is None
        assert display_answer(skipped) == "NA" and not is_timeout(skipped)
        assert 'class="skipped"' in render(subset)
        assert "External reference &lt;bundle&gt; &quot;missing&quot;" in render(subset)
        assert "1 题已运行 · 1 超时 · 1 未运行" in render(subset)
        for invalid_meta in (
            {**meta, "skipped_questions": {}},
            {**meta, "selected_question_ids": ["first-in-zip"]},
            {**meta, "total_questions": 2},
        ):
            with ZipFile(subset_path, "w") as archive:
                for name, content in entries.items():
                    archive.writestr(name, json.dumps(invalid_meta) if name == meta_name else content)
            try:
                import_archive(subset_path)
            except ValueError:
                pass
            else:
                raise AssertionError("Missing results or inconsistent selection metadata must be rejected")

        with ZipFile(path, "a") as archive:
            archive.writestr("benchmark_runs/model-b/questions/duplicate/result.json", json.dumps(result))
        try:
            import_archive(path)
        except ValueError:
            pass
        else:
            raise AssertionError("Duplicate/conflicting result must be rejected")

    # Check every rendered and exported answer against the retained source records.
    data = json.loads((HERE / "results.json").read_text(encoding="utf-8"))
    assert len(data["questions"]) == 100
    models = [m["id"] for m in data["models"]]
    assert len(models) == len(set(models)) and models
    assert len({q["id"] for q in data["questions"]}) == 100
    for model in data["models"]:
        skipped_ids = {q["id"] for q in data["questions"] if q["results"][model["id"]]["status"] == "skipped"}
        assert skipped_ids == set(model["skipped_questions"])
        assert len(data["questions"]) - len(skipped_ids) == model["total_questions"]
    expected = [[q["id"]] + [display_answer(q["results"][m]) for m in models]
                for q in data["questions"]]
    parsed = AnswerTable()
    parsed.feed(render(data))
    assert parsed.cells == [answer for row in expected for answer in row[1:]]
    with (OUT / "answers.csv").open(encoding="utf-8-sig", newline="") as file:
        assert list(csv.reader(file)) == [["question_id"] + models] + expected
    print(f"PASS: ZIP import, declared skips, missing-result validation, timeout handling, "
          f"loop aborts, escaping, and all {len(parsed.cells)} HTML/CSV answers")


if __name__ == "__main__":
    check()
