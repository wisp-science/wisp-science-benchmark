[English](README.md) | **中文**

# wisp-science-benchmark

[Wisp Science](https://github.com/xuzhougeng/wisp-science) 在公开科研 agent benchmark 上的评测。每个子目录是一套。

Benchmark 首页（GitHub Pages）：https://wisp-science.github.io/wisp-science-benchmark/

网站分为两级：首页介绍评测项目，进入子页查看各自结果或评测方案：
[BiomniBench-DA](https://wisp-science.github.io/wisp-science-benchmark/omicos-biomnibench/)、
[CompBioBench 答案表](https://wisp-science.github.io/wisp-science-benchmark/compbiobench/)、
[NatureBench](https://wisp-science.github.io/wisp-science-benchmark/naturebench/)。

CompBioBench 目前展示 100 道题、5 个模型的原始答案，共 495 条运行记录；GPT-6 Astra 的运行配置预先跳过了 5 题。超时、重复工具调用中断和未运行的题目显示为 `NA`（当前 22 条 timeout、5 格未运行），暂不评判正确性。

重建项目页面（首页独立维护，不会被构建脚本覆盖）：

```bash
python compbiobench/reports/build_index.py
python compbiobench/reports/test_build_index.py
python omicos-biomnibench/reports/build_index.py
python -m http.server 8000 --directory docs
```

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
