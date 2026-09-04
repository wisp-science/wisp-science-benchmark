**English** | [中文](README.zh.md)

# wisp-science-benchmark

Evaluations of [Wisp Science](https://github.com/xuzhougeng/wisp-science) on public scientific agent benchmarks. One subdirectory per suite.

Live leaderboard (GitHub Pages): https://wisp-science.github.io/wisp-science-benchmark/

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
