"""Offline profile regression check: python compbiobench/test_profiles.py."""

import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import pandas as pd

import run_benchmark as rb


def main():
    local_ids = ["pooled-infer-donors-q1", "tissue-fibroblast-q1", "odd-one-out-q1",
                 "afgr-1000g-intersect-atac-q1", "genomic-state-q1", "cryptic-exon-q1"]
    all_ids = list(rb.FULL_ONLY_QUESTIONS) + local_ids
    source = pd.DataFrame({"question_id": all_ids, "question": "test", "file_paths": ""})
    selected, skipped = rb.select_questions(source, "default", [local_ids[0], "absent"])
    assert selected.question_id.tolist() == local_ids[1:]
    assert len(skipped) == len(rb.FULL_ONLY_QUESTIONS) + 1
    assert source.question_id.tolist() == all_ids

    with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
        root = Path(tmp)
        csv_path = root / "benchmark.csv"
        source.to_csv(csv_path, index=False)
        args = argparse.Namespace(llm="wisp", model="test", input=str(csv_path),
                                  results_dir=str(root / "runs"), parallel=1, timeout=120,
                                  profile=None, resume=None, list_questions=False,
                                  exclude=[], reverse=False)
        provider = rb.get_provider("wisp")
        runs = {}
        for profile, expected in [(None, local_ids), ("full", all_ids)]:
            scheduled = []
            args.profile = profile
            with patch.object(rb, "check_llm_installed", return_value=(True, "test", "test")), \
                 patch.object(provider, "check_model_available", return_value=(True, "test")), \
                 patch.object(rb, "use_mamba", return_value=False), \
                 patch.object(rb, "ensure_base_conda_env", return_value=True), \
                 patch.object(rb, "prepare_runtime_cache", return_value=None), \
                 patch.object(rb, "run_question", side_effect=lambda i, row, *a: scheduled.append(row.question_id)):
                rb.cmd_run(args)
            assert scheduled == expected
            profile = profile or "default"
            metadata_path, = (root / "runs").glob(f"*_{profile}_*/run_metadata.json")
            meta = json.loads(metadata_path.read_text())
            assert meta["benchmark_profile"] == profile
            assert meta["selected_question_ids"] == expected
            assert meta["input_questions"] == len(all_ids)
            assert meta["total_questions"] == len(expected)
            assert set(meta["skipped_questions"]) == set(all_ids) - set(expected)
            runs[profile] = metadata_path

        # Listing is offline; resume inherits its profile and rejects narrowing.
        with patch.object(rb, "check_llm_installed", side_effect=AssertionError("must stay offline")):
            args.list_questions = True
            args.profile = None
            args.resume = runs["full"].parent.name
            output = io.StringIO()
            with redirect_stdout(output):
                rb.cmd_run(args)
            assert "Benchmark-full" in output.getvalue()
            assert output.getvalue().count("[SELECT]") == len(all_ids)
            args.profile = "default"
            try:
                rb.cmd_run(args)
            except ValueError as exc:
                assert "Cannot resume" in str(exc)
            else:
                raise AssertionError("profile switch accepted")

            args.resume, args.profile = runs["default"].parent.name, None
            with patch.object(rb, "BENCHMARK_PROFILE_VERSION", rb.BENCHMARK_PROFILE_VERSION + 1):
                try:
                    rb.cmd_run(args)
                except ValueError as exc:
                    assert "membership changed" in str(exc)
                else:
                    raise AssertionError("changed membership accepted on resume")
                args.profile = "full"
                output = io.StringIO()
                original_meta = runs["default"].read_bytes()
                with redirect_stdout(output):
                    rb.cmd_run(args)
                assert output.getvalue().count("[SELECT]") == len(all_ids)
                assert runs["default"].read_bytes() == original_meta, "Listing must not modify metadata"

            # A pre-profile run must still resume as full.
            legacy = root / "runs" / "wisp_test_legacy"
            legacy.mkdir()
            legacy_meta = json.loads(runs["full"].read_text())
            del legacy_meta["benchmark_profile"]
            del legacy_meta["benchmark_profile_version"]
            legacy_meta["timestamp"] = "legacy"
            (legacy / "run_metadata.json").write_text(json.dumps(legacy_meta))
            args.resume, args.profile = legacy.name, None
            output = io.StringIO()
            with redirect_stdout(output):
                rb.cmd_run(args)
            assert "Benchmark-full" in output.getvalue()

        # Both multi-model and run-all entry points must forward the profile.
        args.profile, args.model, args.resume = "full", "test1,test2", None
        args.rerun = [local_ids[0]]
        with patch.object(rb, "_run_single_model") as run:
            rb.cmd_run(args)
            assert len(run.call_args_list) == 2
            assert all(call.args[0].profile == "full" for call in run.call_args_list)
            assert all(call.args[0].rerun == [local_ids[0]] for call in run.call_args_list)
        args.output, args.profile, args.resume = str(root / "all.csv"), None, legacy.name
        with patch.object(rb, "check_llm_installed", return_value=(True, "test", "test")), \
             patch.object(rb, "cmd_run") as run, patch.object(rb, "cmd_merge") as merge:
            rb.cmd_run_all(args)
            assert run.call_args_list
            assert all(call.args[0].profile == "full" for call in run.call_args_list)
            assert merge.call_args.args[0].profile == "full"
            assert all(call.args[0].rerun == [local_ids[0]] for call in run.call_args_list)
        args.rerun = []

        # Merge must filter both the task rows and the source run columns.
        for profile, metadata_path in runs.items():
            qid = local_ids[0] if profile == "default" else all_ids[0]
            question_dir = metadata_path.parent / "questions" / qid
            question_dir.mkdir(parents=True)
            result = {"question_id": qid, "output": {"answer": profile},
                      "execution": {"elapsed_time": 1}, "cost": {"total_usd": 0},
                      "usage": {"input_tokens": 1, "output_tokens": 1}}
            (question_dir / "result.json").write_text(json.dumps(result))
        for profile, expected, run_count in [("default", local_ids, 1), ("full", all_ids, 2)]:
            merged = root / f"{profile}.csv"
            rb.cmd_merge(argparse.Namespace(input=str(csv_path), runs_dir=args.results_dir,
                                            output=str(merged), profile=profile))
            frame = pd.read_csv(merged)
            assert frame.question_id.tolist() == expected
            answer_columns = [c for c in frame if c.startswith("answer_")]
            assert len(answer_columns) == run_count
            assert set(frame[answer_columns].stack().dropna()) == {profile}

        # Promote in place: preserve successes, retry errors, and fill missing/full-only tasks.
        promotion_root = root / "promotion"
        promotion_run = promotion_root / "wisp_test_default_original"
        promotion_run.mkdir(parents=True)
        metadata_path = promotion_run / "run_metadata.json"
        old_meta = json.loads(runs["default"].read_text())
        metadata_path.write_text(json.dumps(old_meta))
        preserved = {}

        def save_result(qid, status="success"):
            path = promotion_run / "questions" / qid / "result.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "question_id": qid, "status": status,
                "output": {"answer": qid if status == "success" else "ERROR: timeout"},
                "execution": {"elapsed_time": 1}, "cost": {"total_usd": 0},
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }))
            return path

        for qid in local_ids[:-1]:
            path = save_result(qid, "error" if qid == local_ids[0] else "success")
            if qid != local_ids[0]:
                preserved[path] = path.read_bytes()
        promotion_args = argparse.Namespace(**{
            **vars(args), "model": "test", "resume": promotion_run.name,
            "results_dir": str(promotion_root), "profile": "full", "list_questions": False,
        })
        scheduled = []

        def execute_question(i, row, *unused):
            scheduled.append(row.question_id)
            save_result(row.question_id)

        with patch.object(rb, "check_llm_installed", return_value=(True, "test", "test")), \
             patch.object(provider, "check_model_available", return_value=(True, "test")), \
             patch.object(rb, "use_mamba", return_value=False), \
             patch.object(rb, "ensure_base_conda_env", return_value=True), \
             patch.object(rb, "prepare_runtime_cache", return_value=None), \
             patch.object(rb, "run_question", side_effect=execute_question):
            rb.cmd_run(promotion_args)
            assert scheduled == list(rb.FULL_ONLY_QUESTIONS) + [local_ids[0], local_ids[-1]]
            meta = json.loads(metadata_path.read_text())
            assert meta["benchmark_profile"] == "full"
            assert meta["selected_question_ids"] == all_ids
            assert meta["total_questions"] == len(all_ids) and meta["skipped_questions"] == {}
            assert meta["questions_to_run"] == len(scheduled)
            assert all(path.read_bytes() == content for path, content in preserved.items())

            # Even if all results already exist, an explicit upgrade must persist full metadata.
            metadata_path.write_text(json.dumps(old_meta))
            scheduled.clear()
            rb.cmd_run(promotion_args)
            assert scheduled == []
            assert json.loads(metadata_path.read_text())["benchmark_profile"] == "full"
            assert json.loads(metadata_path.read_text())["questions_to_run"] == 0
            promotion_args.profile = None
            rb.cmd_run(promotion_args)
            assert scheduled == []
            assert json.loads(metadata_path.read_text())["benchmark_profile"] == "full"

        # A folder still named default must merge according to its promoted metadata.
        merged = root / "promoted-full.csv"
        rb.cmd_merge(argparse.Namespace(input=str(csv_path), runs_dir=str(promotion_root),
                                        output=str(merged), profile="full"))
        frame = pd.read_csv(merged)
        answer_column, = [column for column in frame if column.startswith("answer_")]
        assert frame.question_id.tolist() == all_ids
        assert frame[answer_column].tolist() == all_ids

        # Force only a selected success; unrelated failed/missing questions must stay untouched.
        target = local_ids[1]
        failed_path = save_result(local_ids[0], "error")
        missing_path = promotion_run / "questions" / local_ids[-1] / "result.json"
        missing_path.unlink()
        untouched = {p: p.read_bytes() for p in promotion_run.glob("questions/*/result.json")
                     if p.parent.name != target}
        promotion_args.rerun = [target]
        scheduled.clear()
        with patch.object(rb, "check_llm_installed", return_value=(True, "test", "test")), \
             patch.object(provider, "check_model_available", return_value=(True, "test")), \
             patch.object(rb, "use_mamba", return_value=False), \
             patch.object(rb, "ensure_base_conda_env", return_value=True), \
             patch.object(rb, "prepare_runtime_cache", return_value=None), \
             patch.object(rb, "run_question", side_effect=execute_question):
            rb.cmd_run(promotion_args)
            assert scheduled == [target]
            assert all(p.read_bytes() == content for p, content in untouched.items())
            assert not missing_path.exists()
            assert json.loads(failed_path.read_text())["status"] == "error"
            meta = json.loads(metadata_path.read_text())
            assert meta["selected_question_ids"] == all_ids
            assert meta["total_questions"] == len(all_ids)
            assert meta["questions_to_run"] == 1 and meta["rerun_question_ids"] == [target]

            promotion_args.rerun = [target, local_ids[0], target]
            scheduled.clear()
            rb.cmd_run(promotion_args)
            assert scheduled == local_ids[:2], "Duplicate IDs must execute only once"
            assert not missing_path.exists()

        with patch.object(rb, "check_llm_installed", side_effect=AssertionError("must stay offline")):
            preview = argparse.Namespace(**{**vars(promotion_args), "list_questions": True, "rerun": [target]})
            before = metadata_path.read_bytes()
            output = io.StringIO()
            with redirect_stdout(output):
                rb.cmd_run(preview)
            assert output.getvalue().count("[SELECT]") == 1 and f"[SELECT] {target}" in output.getvalue()
            assert metadata_path.read_bytes() == before
            invalid = [
                ({"resume": None}, "requires --resume"),
                ({"rerun": ["typo-question-id"]}, "Unknown --rerun"),
                ({"exclude": [target]}, "excluded by profile or --exclude"),
                ({"results_dir": args.results_dir, "resume": runs["default"].parent.name,
                  "profile": "default", "rerun": [all_ids[0]]}, "excluded by profile or --exclude"),
            ]
            for overrides, message in invalid:
                bad_args = argparse.Namespace(**{**vars(preview), **overrides})
                try:
                    rb.cmd_run(bad_args)
                except ValueError as exc:
                    assert message in str(exc)
                else:
                    raise AssertionError(f"Invalid rerun accepted: {overrides}")
            assert metadata_path.read_bytes() == before
    print("ok: profiles, upgrades, targeted reruns, preserved results and population, validation, and merges")


if __name__ == "__main__":
    main()
