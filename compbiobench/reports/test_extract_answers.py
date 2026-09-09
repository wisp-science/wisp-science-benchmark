"""python reports/test_extract_answers.py"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from extract_answers import load_cells, write_long, write_wide


def main() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        run = root / "wisp_demo_full_1"
        qdir = run / "questions" / "toy-q1"
        qdir.mkdir(parents=True)
        (run / "run_metadata.json").write_text(json.dumps({
            "model": "demo-model", "timestamp": "1",
        }))
        (qdir / "result.json").write_text(json.dumps({
            "question_id": "toy-q1",
            "status": "success",
            "execution": {"elapsed_time": 1.5},
            "output": {"answer": "OK"},
        }))
        models, cells = load_cells(root)
        assert models == ["demo-model"]
        assert cells["toy-q1"]["demo-model"]["answer"] == "OK"
        wide = root / "wide.csv"
        write_wide(wide, models, cells)
        assert "toy-q1,OK" in wide.read_text(encoding="utf-8-sig")
        long_path = root / "long.tsv"
        write_long(long_path, models, cells)
        assert "toy-q1\tdemo-model" in long_path.read_text(encoding="utf-8-sig")
    print("ok: extract_answers reads result.json into wide and long tables")


if __name__ == "__main__":
    main()
