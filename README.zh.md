[English](README.md) | **中文**

# wisp-science-benchmark

[Wisp Science](https://github.com/xuzhougeng/wisp-science) 在公开科研 agent benchmark 上的评测。每个子目录是一套。

在线榜单（GitHub Pages）：https://wisp-science.github.io/wisp-science-benchmark/

| 套件 | Benchmark |
| --- | --- |
| [omicos-biomnibench](omicos-biomnibench/README.md) | [BiomniBench-DA](https://huggingface.co/datasets/phylobio/BiomniBench-DA) |
| [compbiobench](compbiobench/README.md) | [CompBioBench](https://github.com/Genentech/compbiobench-runner) |
| [naturebench](naturebench/README.md) | [NatureBench](https://github.com/FrontisAI/NatureBench) |

不同套件的分数 **不能** 横比。

| | BiomniBench-DA | CompBioBench | NatureBench |
| --- | --- | --- | --- |
| 题型 | 生信数据分析 | 计算生物学问答 | 科学 ML 编程，对论文 SOTA |
| 评分 | rubric LLM judge | 单行 exact match | 隐藏测试集 + HTTP eval + 事后 judge |
| 环境 | 本地 workspace | 每题 clone conda | Docker + 多数题要 GPU |
| 时限 | 分钟级 | 默认 2h | 官方 4 小时/题 |
| 规模 | 50 题 | ~100 题 | 90 题，或 NatureBench-25 |
