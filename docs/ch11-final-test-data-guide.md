# 独立最终测试集制作指南

> **当前项目决策（2026-10-23）：暂不制作新的独立测试集。** 继续使用 `tests/data/eval_ch11_failure_regression.jsonl` 和 `tests/data/eval_ch11_failure_challenge.jsonl` 做开发/项目验收对比，并在报告中注明数据已用于优化，不称为独立发布测试。本指南后续的“新建独立集”步骤暂不执行；如未来需要对外宣称独立发布验收，再单独恢复该流程。

最终门禁必须使用一份全新人工整理的数据集。当前 `tests/data/eval_ch11_test.jsonl` 是历史评估集拆分结果，曾用于调参，不能当作独立最终测试集。

已从已知 Recall@5 漏召题扩出开发回归候选集 `tests/data/eval_ch11_failure_regression.jsonl`。它可用来针对性观察失败修复，但来自现有 validation 错题，且变体标签仍待人工核验，不能替代独立最终测试集。

在该回归集上又构建了 30 题挑战集 `tests/data/eval_ch11_failure_challenge.jsonl`，覆盖同 6 个失败主题的多个组合问法。它同样是从已知失败派生的开发集，不能作为发布门禁的独立 test 数据；请先按 `data/ch11/failure_challenge_manifest.json` 核对来源、哈希和待人工复核状态。

## 1. 数据集格式

每行一个 JSON 对象，UTF-8 编码。沿用 `tests/data/eval_ch04.jsonl` 的字段：

```json
{"id":"FT-A-001","bucket":"A_policy","query":"订单未满包邮门槛时收多少运费？","expect_section":["运费与包邮"],"expect_points":["未满 99 元收取 6 元基础运费"],"should_refuse":false}
```

字段要求：

- `id`：新建且唯一的稳定编号，不复用历史评估集 ID。
- `bucket`：必须属于 `A_policy`、`B_model`、`C_colloquial`、`E_multi`、`D_absent`。
- `query`：真实用户会问的自然语言问题；口语类应保留口语表达。
- 可回答问题填写 `expect_section` 和 `expect_points`，并设 `should_refuse` 为 `false`。
- 跨文档问题还应填写 `expect_sections_all`，每个内层数组表示必须命中的一组来源章节。
- `D_absent` 问题应确认现有知识源确实无答案，填写空 `expect_section`/`expect_points`，并设 `should_refuse` 为 `true`。

这里的“拒答”是指证据不足时不编造确定的业务事实，不代表客服必须沉默，也不能把知识库没有提到的功能直接判定为“不支持”。合适的处理可以是说明当前资料未说明、请求必要信息或转人工核实。D_absent 优先覆盖当前系统确实拿不到的数据（如指定订单的实时物流、个人账户积分余额），避免把“未记载”误当成否定规则。

为避免评分歧义，D_absent 候选项可以增加 `expected_handling`，写清楚它应说明“资料未说明”、只能回答已有依据的部分，还是需要人工核实；不应把“拒答”评分成必须输出简单的“是/否”或完全不回答。

五类都要有样本，不能用一个分桶的大量样本掩盖其它分桶缺失。每个答案标签都要由审核者对照当前 KB 原文核验；冲突、过时或无法确定的题目不要放进最终集。

## 2. 独立性和来源记录

1. 从现有评估集、历史报告和候选错误样本之外重新收集/撰写问题；不要复制历史题目后只改写措辞。
2. 在候选配置和代码冻结后制作最终集；测试集不得用于提示词、检索参数、知识文档或门槛调优。
3. 为每个 ID 记录题目来源与答案依据，例如 `FT-A-001: 客服工单批次 2026-10; data/kb/billing-shipping.md#运费与包邮`。无来源的合成问题须明确记录其业务场景来源和人工审核依据。
4. 用人工审核者的真实标识填写 `curator_id`；不要填模型名称或占位字符串。脱敏原始工单和个人信息。
5. 保存 JSONL 后计算 SHA-256，填入 `data/ch11/test_dataset_provenance.example.json` 的副本；确认 `derived_from_existing_eval_data`、`previously_evaluated`、`used_for_candidate_tuning` 均为 `false`，`dataset_role` 为 `test`，`curation_method` 为 `manual`。

## 3. 验收命令

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = '.'
$env:NO_PROXY = 'localhost,127.0.0.1,api.siliconflow.cn'

uv run --python 3.12 python scripts/eval_candidates.py `
  --dataset tests/data/eval_ch11_test_independent.jsonl `
  --dataset-role test `
  --dataset-provenance data/ch11/test_dataset_provenance.json `
  --knowledge-version <当前 Milvus 知识快照标识> `
  --candidate baseline --candidate recall_20 --candidate rerank_5 `
  --out data/ch11/reports/eval_candidates_independent_test.json
```

用至少三次同代码、同模型、同知识快照的 validation baseline 校准相对波动；运营方还需明确 `min_samples`、不可回答问题拒答率下限和可回答问题误拒率上限。获得这些值后，调用 `scripts/calibrate_release_thresholds.py` 生成真实门槛文件，再对独立测试报告运行 `scripts/eval_release_gate.py`。只有完整来源清单、版本匹配且门槛全部齐备，门禁才可能给出 `PASS` 或 `FAIL`；缺项时应保持 `INCONCLUSIVE`。

### 最终答复质量评估

对独立集运行现有 `eval_ch04.py`，在冻结的三个检索候选下分别评估答案覆盖度、Faithfulness 和处理方式：

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = '.'
$env:NO_PROXY = 'localhost,127.0.0.1,api.siliconflow.cn'

uv run --python 3.12 python scripts/eval_ch04.py `
  --dataset tests/data/eval_ch11_test_independent.jsonl `
  --dataset-role test `
  --dataset-provenance data/ch11/test_dataset_provenance.json `
  --knowledge-version <当前 Milvus 知识快照标识> `
  --candidate baseline --candidate recall_20 --candidate rerank_5 `
  --out-json data/ch11/reports/eval_answers_independent_candidates.json `
  --out-txt data/ch11/reports/eval_answers_independent_candidates.txt `
  --skip-read-notes
```

有 `expected_handling` 的题会额外评估处理方式合规率和无依据断言率，并保留逐题答案、证据和裁判理由供人工抽查。LLM 裁判结果需抽查校准后才能作为硬门槛。`confidence_gate_refusal_rate` 只代表检索置信度闸；最终答复行为由独立的 handling 指标评估。

答案质量报告现已接入发布门禁。检索报告和答案报告必须来自同一测试集、知识快照、代码指纹、模型和候选配置。先基于至少三次完整 validation baseline 运行生成校准记录，再把校准记录与经运营确认的绝对门槛合入实际门槛文件；独立测试集不能用于调参或校准。对要发布的候选运行：

```powershell
$env:PYTHONPATH = '.'
uv run --python 3.12 python scripts/eval_release_gate.py `
  --report data/ch11/reports/eval_candidates_independent_test.json `
  --candidate baseline `
  --thresholds data/ch11/release_thresholds.json `
  --answer-report data/ch11/reports/eval_answers_independent_candidates.json
```

将 `--candidate` 同时改为 `recall_20` 或 `rerank_5` 可评估相应候选。`data/ch11/release_thresholds_currentcode_draft.json` 是绑定当前 validation 校准指纹的门槛草稿，运营绝对阈值仍为空，不能据此发布；缺少阈值、人审或版本匹配时，门禁应保持 `INCONCLUSIVE`。

三候选的 D 类答复和裁判结论导出到 `data/ch11/final_answer_handling_candidates_review.csv`（每候选 10 行）；审核者可逐条抽查并填写 `review_status`、`reviewer_id` 和 `review_note`，用于校验 LLM 裁判是否可靠。旧的 `data/ch11/final_answer_handling_review.csv` 只对应先前 baseline 答复报告。
