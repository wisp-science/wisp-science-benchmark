"""python compbiobench/reports/test_select_rerun.py"""

from pathlib import Path
from tempfile import TemporaryDirectory

from select_rerun import classify, is_missing, select_rerun_questions, write_outputs


def result(answer, status="success"):
    return {"answer": answer, "status": status}


def test_select_rerun_questions():
    data = {
        "models": [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}, {"id": "e"}],
        "questions": [
            {
                "id": "agree-4",
                "results": {
                    "a": result("X"), "b": result("X"), "c": result("X"),
                    "d": result("X"), "e": result("Y"),
                },
            },
            {
                "id": "weak-2",
                "results": {
                    "a": result("P"), "b": result("P"), "c": result("Q"),
                    "d": result("Q"), "e": result("ERROR: timeout", "error"),
                },
            },
            {
                "id": "all-timeout",
                "results": {
                    "a": result("ERROR: timeout", "error"),
                    "b": result("ERROR: timeout", "error"),
                    "c": result("NA", "skipped"),
                    "d": result("ERROR: api down", "error"),
                    "e": result("ERROR: timeout", "error"),
                },
            },
            {
                "id": "encode-atac-pipeline-q1",
                "results": {
                    "a": result("29597"), "b": result("29597"), "c": result("29597"),
                    "d": result("29597"), "e": result("29597"),
                },
            },
        ],
    }
    rows = select_rerun_questions(data)
    ids = [row["question_id"] for row in rows]
    assert ids == ["weak-2", "all-timeout", "encode-atac-pipeline-q1"]
    assert rows[-1]["reason"] == "default-skipped" and rows[-1]["max_agree"] == 5
    assert rows[0]["reason"] == "agreement-below-3" and rows[0]["max_agree"] == 2
    assert rows[1]["reason"] == "no-completed-answer" and rows[1]["max_agree"] == 0
    assert is_missing(result("ERROR: timeout", "error"))
    assert not is_missing(result("NA"))
    assert classify(2) == "agreement-below-3"
    with TemporaryDirectory() as tmp:
        txt, tsv = write_outputs(rows, Path(tmp))
        assert txt.read_text(encoding="utf-8").splitlines() == ids
        assert "weak-2" in tsv.read_text(encoding="utf-8")


def main():
    test_select_rerun_questions()
    print("ok: rerun selection flags weak/unfinished questions and default-profile skips")


if __name__ == "__main__":
    main()
