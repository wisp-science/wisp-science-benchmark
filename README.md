**English** | [中文](README.zh.md)

# wisp-science-benchmark

Evaluations of [Wisp Science](https://github.com/xuzhougeng/wisp-science) on public scientific agent benchmarks. One subdirectory per suite.

Benchmark hub (GitHub Pages): https://wisp-science.github.io/wisp-science-benchmark/

The homepage introduces the benchmark suites and links to their project pages:
[BiomniBench-DA](https://wisp-science.github.io/wisp-science-benchmark/omicos-biomnibench/),
[CompBioBench answers](https://wisp-science.github.io/wisp-science-benchmark/compbiobench/), and
[NatureBench](https://wisp-science.github.io/wisp-science-benchmark/naturebench/).

CompBioBench currently displays 100 questions × 5 models (500 run records).
Cells show original answers; timeouts, repeated-tool-loop aborts, and skipped
questions display `NA` (17 timeouts in the latest table).
Correctness has not been evaluated.

Rebuild the project pages (the homepage is maintained separately):

```bash
python compbiobench/reports/build_index.py
python compbiobench/reports/test_build_index.py
python omicos-biomnibench/reports/build_index.py
python -m http.server 8000 --directory docs
```

| Suite | Benchmark |
| --- | --- |
| [omicos-biomnibench](omicos-biomnibench/README.md) | [BiomniBench-DA](https://huggingface.co/datasets/phylobio/BiomniBench-DA) |
| [compbiobench](compbiobench/README.md) | [CompBioBench](https://github.com/Genentech/compbiobench-runner) |
| [naturebench](naturebench/README.md) | [NatureBench](https://github.com/FrontisAI/NatureBench) |

Scores from different suites are **not** comparable.

| | BiomniBench-DA | CompBioBench | NatureBench |
| --- | --- | --- | --- |
| Task type | Biomedical data analysis | Computational biology Q&A | Scientific ML coding vs paper SOTA |
| Scoring | rubric LLM judge | one-line exact match | hidden test set + HTTP eval + post-hoc judge |
| Environment | local workspace | per-question conda clone | Docker; most tasks need GPU |
| Time budget | minutes | 2 h default | official 4 h / task |
| Scale | 50 tasks | ~100 tasks | 90 tasks, or NatureBench-25 |
