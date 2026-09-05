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

        # Listing is offline; resume inherits its profile and rejects a switch.
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
        with patch.object(rb, "_run_single_model") as run:
            rb.cmd_run(args)
            assert len(run.call_args_list) == 2
            assert all(call.args[0].profile == "full" for call in run.call_args_list)
        args.output, args.profile, args.resume = str(root / "all.csv"), None, legacy.name
        with patch.object(rb, "check_llm_installed", return_value=(True, "test", "test")), \
             patch.object(rb, "cmd_run") as run, patch.object(rb, "cmd_merge") as merge:
            rb.cmd_run_all(args)
            assert run.call_args_list
            assert all(call.args[0].profile == "full" for call in run.call_args_list)
            assert merge.call_args.args[0].profile == "full"

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
            assert set(frame[answer_columns].stack()) == {profile}
    print("ok: selection, execution metadata, offline listing, resume, and separate merges")


if __name__ == "__main__":
    main()
