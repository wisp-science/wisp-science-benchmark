"""Run with python compbiobench/reports/test_build_index.py (stdlib only)."""

import csv
import json
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from zipfile import ZipFile

from build_index import HERE, OUT, display_answer, import_archive, is_timeout, render


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
    assert len(models) == len(set(models)) == 4
    assert len({q["id"] for q in data["questions"]}) == 100
    expected = [[q["id"]] + [display_answer(q["results"][m]) for m in models]
                for q in data["questions"]]
    parsed = AnswerTable()
    parsed.feed(render(data))
    assert parsed.cells == [answer for row in expected for answer in row[1:]]
    with (OUT / "answers.csv").open(encoding="utf-8-sig", newline="") as file:
        assert list(csv.reader(file)) == [["question_id"] + models] + expected
    print("PASS: ZIP import, alignment, timeout handling, escaping, and all 400 HTML/CSV answers")


if __name__ == "__main__":
    check()
