# 合成案例：模型榜单口径核验

日期：2026-11-01 · 状态：已完成 (complete) · 运行：f8c27729e8da4acf

记录类型：合成演示 · 合成演示：内容与决策为离线回放，不是实际情报或真实模型能力评估。

本次记录：1 项发现、1 条断言、2 份来源；用时 0 秒。

> **离线演示：所有发现、网页和模型决策均为合成测试数据，不是实际情报。**

## 覆盖与信源状态

| 调研方向 | 扫描状态 |
| --- | --- |
| Benchmark | 正常 |
| 竞品 | 正常 |
| 人才 | 正常 |
| 供应商 | 正常 |

| 发现通道 | 状态 | 保留条目 | 日期过滤 |
| --- | --- | ---: | ---: |
| 全网检索 | 正常 | 4 | 0 |
| 论文 | 正常 | 0 | 0 |
| GitHub | 正常 | 0 | 0 |
| HuggingFace | 正常 | 0 | 0 |
| RSS 订阅 | 正常 | 0 | 0 |

## 研究发现

- **benchmark**：原始结果与全榜第一的新闻表述冲突：模型 A 仅在开放模型子榜单领先。 `claim_61db7384b3c54908`

## 数据储备建议

- **P1**：对齐榜单分类后再安排评测样本覆盖检查。；依据：`claim_61db7384b3c54908`；不确定性：演示数据，不用于实际业务决策。

## 断言与原始证据

### claim_61db7384b3c54908 · contradict

Model A ranks first overall on CodeBench v2.

The original result distinguishes overall and open-model rankings.

- [Synthetic official CodeBench v2 results](https://research-demo.invalid/model-a) · `doc_224fc0973b3f45dbfc3c` · full_text · 发布时间：未知
  > Model B leads the overall CodeBench v2 ranking. Model A leads only the open-model subset.

## 未解决问题


## 风险与质量评审

- 本次为合成回放，不能用于估计真实模型准确率或运行成本。
- Critic：`{"factuality": 8, "completeness": 8, "citations": 8, "timeliness": 8, "actionability": 8, "notes": []}`
- 合成轨迹保留了榜单分类与证据，实际效果仍需真实案例评测。

## 运行审计

- 工具/控制步骤：7
- 模型估算费用（含未确认请求保留额度）：$0.000000；非供应商账单。
- Tavily 基础搜索 credits（含未确认请求）：0
- 已返回 token：输入 0，输出 0；思考 token 为输出的子集。
- 停止原因：model_finished
- 语义判定包含模型评估；数值置信度不是经校准的准确率。
