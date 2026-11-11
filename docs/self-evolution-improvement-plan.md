# 智能客服自进化机制优化方案（修订版）

> **目标：** 在保留现有客服、RAG、诊断和人工审核代码的基础上，减少逐条处理工作，让知识或配置修改可追溯、可比较、可恢复。
>
> **边界：** 自进化指由线上问题驱动的诊断、归并、候选生成和离线评估闭环；不等于在线训练模型，也不允许模型未经审核直接修改生产知识库。
>
> **数据集决策：** 继续使用已有 18 条回归集和 30 条挑战集，不另建独立最终测试集。它们已参与开发与修复，只能用于项目回归和验收，不能作为独立泛化测试通过的证据。

## 1. 参考实现

| 参考 | 借鉴点 | 不照搬的部分 |
|---|---|---|
| [Langfuse 用户反馈到评估集](https://langfuse.com/resources/engineering/user-feedback-to-evaluation-datasets) | 反馈关联 Trace；把有代表性的失败转成评估项；固定数据版本跑实验；评估器与人工判断不一致时进入标注队列 | 不把点赞/点踩直接当事实标签；不把评估集自动写回 RAG |
| [STACKFEED](https://aclanthology.org/2025.emnlp-industry.176.pdf) | 集中分析角色聚类失败并定位相关文档；结构化编辑候选；按批次评估知识库状态 | 研究框架不等于已经验证的客服生产系统；项目先生成编辑草案，不自动发布 |
| [SIARE](https://github.com/synaptiai/siare) | 把提示词、检索参数和 Agent 拓扑作为候选，在质量、延迟、成本间实验比较 | 先不引入自动拓扑搜索；项目只比较有限候选参数 |

## 2. 当前代码基础与状态

| 能力 | 当前状态 |
|---|---|
| 线上诊断案例、Trace 和检索证据关联 | 已有 `diagnosis_cases` 及诊断 API；低置信度、工具失败、用户负反馈作为待调查信号 |
| 人工确认故障类型、类别抽样和风险升级 | 已有状态字段、审核接口和类别审核策略 |
| 已确认案例生成受限检索候选 | 已有接口；达到案例数门槛后，召回/重排类别可提出候选，不改线上配置 |
| 低置信问题归并和知识审核队列 | 已有 `low_confidence_questions`、`review_queue`；经人工批准后才写知识库 |
| Retrieval evaluation | Candidate reports now include pre-rerank Recall@20 plus post-rerank Recall@5/MRR, with the collection name recorded; runtime results remain pending |
RoBERTa is multilabel: report Micro-F1, Macro-F1, per-class Precision/Recall/F1/support, Hamming Loss (incorrect label cells divided by samples times labels), and Subset Accuracy (exact label-set match rate), with thresholds and per-class sample counts. The evaluator now emits these metrics; runtime acceptance still requires the trained ONNX artifacts.
| 分批归并与按需复核 | 已加入代码：每批最多 10 条，归并角色先处理，仅对拟合并项调用复核角色；仍待实际运行验收 |
| RoBERTa 主题分桶与证据约束诊断提议 | 已加入批处理入口：分类器制品哈希和分析配置版本绑定任务范围；模型按案例输出可校验的多故障提议，不能自行确认；仍待迁移与真实服务验收 |
| Knowledge/config/release snapshots and compatible rollback | Code now includes immutable snapshot/config/release catalogs, Milvus active-alias switching, compatibility checks, and rollback commands; migrations and live rollback acceptance remain pending |

## 3. 修订后的完整闭环

```text
线上请求与分层抽样审核
  → 保存脱敏 Trace、检索前后结果、最终证据、回答、工具状态和配置版本
  → 收集低置信度、工具异常、用户反馈，以及高风险/高流量类别抽样案例
  → 确定性规则记录故障组件、错误码/超时、依赖健康状态和重试结果；仅满足组件级规则的异常确认为基础设施故障
  → RoBERTa 复用现有多标签主题分类模型，输出标签和置信度，用于分桶
  → 主分析大模型一次调用，分别输出相似案例归并建议和初步故障归因
  → Workflow 按证据完整性、规则冲突和风险路由，不依赖未经校准的模型自报置信度
      ├─ 基础设施故障：记录并汇总到基础设施问题队列；只跳过受影响的操作，其他诊断继续
      ├─ 知识缺失：基于权威来源生成知识草案
      ├─ 召回/重排/回答问题：先核对知识入库、分块、索引、过滤和嵌入版本，再生成有限参数候选
      ├─ 工具/路由问题：检查参数、权限、接口结果或路由配置
      └─ 证据不足/冲突：先尝试从已有系统补取证据；来源核查完成但证据仍不足则标记 inconclusive；补取执行失败则重试，耗尽后标记 blocked 并转人工，不生成定向修复
  → 仅对拟合并组调用归并复核；补证后仍存在归因冲突或高风险时才调用诊断复核
  → 人工审批候选；冲突组拆分，不强行合并
  → 在固定数据和基线版本上做单变量离线对照，记录所有实验、成本与失败样例
  → 通过发布门禁后，根据变更类型创建或选定知识快照与系统配置版本
  → 每次发布创建 release_id，绑定实际版本组合并切换生产环境
  → 发布后做查询冒烟与回归验证；异常时恢复上一稳定发布组合并复验
```

发布不要求每次都创建新的知识快照：知识内容未变时复用当前快照；系统配置未变时复用当前配置版本。但每次实际发布都生成新的 `release_id`，记录当时生效的组合。

点赞、点踩、低检索分数都只是线索，不能单独证明答复错误或知识缺失。为了发现用户未报告的自信错误，除现有信号外，还要按业务类别、请求量和风险分层抽样，重点检查高风险回答、证据不足却正常作答的案例，以及检索证据与最终回答不一致的案例。报告中区分各问题来源及其占比。

## 4. 故障诊断与证据记录

诊断分两步：先由确定性 Workflow 节点记录发生组件、错误码/异常类型、依赖服务、健康检查和重试结果，并依据预先定义的组件级规则确认可确定的运行故障；单次超时不自动等于基础设施故障。其余案例由主分析大模型结合 Trace 与检索证据给出初步归因。模型负责综合解释，不替代可直接观测的系统状态。归并结论和故障归因是两个独立结果，必须分别持久化状态、校验和验收；一次结果失败只重跑对应任务，不丢弃另一项已经通过校验的结果。

### 4.1 每条诊断案例保留的信息

- 稳定案例 ID、发生时间、脱敏后的问题和必要上下文、反馈来源及业务类别/风险级别。
- 实际知识版本、代码提交、提示词版本、模型/重排模型版本和检索配置。
- 召回阶段候选文档 ID、分数、顺序；重排阶段输入、输出、分数和顺序。
- 故障组件、依赖服务、错误码/异常类型、健康检查结果、重试次数与结果；区分向量库、业务工具、模型服务和应用内部故障。
- 实际传给生成模型的证据片段、最终回答、工具调用与结果、拒答/转人工状态。
- 每个诊断子任务（按组件或待判断问题划分）的独立 `execution_status`、`outcome_proposed`、`outcome_final` 和零个或多个故障项；每项保存类型、证据与理由、`proposal_source`、`review_source`、提出/确认/驳回状态及 `confirmation_source`。另保存人工复核标签、复核者和时间。案例级独立保存 `case_diagnosis_status`，由子任务状态汇总，不覆盖子任务明细。

### 4.2 归因边界

| 类型 | 判定依据 |
|---|---|
| `knowledge_gap` | 权威来源中确实没有适用规则；需核对来源后确认，不能仅凭检索空结果判定 |
| `indexing_error` | 权威知识存在，但未进入当前有效索引，或分块、嵌入版本、元数据/权限过滤与预期不符 |
| `retrieval_miss` | 确认权威知识已进入当前有效索引后，相关内容仍不在重排前召回候选中 |
| `rerank_error` | 正确内容进入召回候选，但重排后缺失或排名不足 |
| `generation_error` | 正确且充分的证据已传入生成上下文，但回答漏答、矛盾或无依据扩写 |
| `tool_error` / `routing_error` | 工具执行或请求路由的证据明确指向该环节 |
| `infrastructure_error` | 组件级错误信号和预先定义的确认规则共同指向基础设施故障；单独出现超时只证明操作未按时完成，不能据此确认基础设施故障 |
| `unknown` | 证据缺失、冲突或不足以区分原因；保留待查，不生成定向知识修改 |

按组件规则确认的基础设施事件只暂停依赖故障服务的操作，不能终止整条案例。如果同一请求同时有超时和错误回答，应保留异常记录，并继续用已经存在的 Trace、检索证据和回答记录检查可诊断的问题。证据源检查已完成但证据不存在或不足时，子任务可完成并将 `outcome_final` 置为 `inconclusive`；若证据补取调用本身失败，则将执行状态标为 `retryable`，超过重试上限后标为 `failed`，不能伪装成诊断完成。相同基础设施错误按时间窗口汇总频率、影响范围和失败率，再升级处理，不能静默丢弃。

故障项的提出、复核和确认来源分开管理：持久化 `proposal_source`（如 `model`、`rule`）、`review_source`（如 `model`、`human`）及故障项 `status`（`proposed`、`confirmed`、`rejected`）。确定性 Workflow 规则只有在组件、依赖服务、错误码/异常类型、健康检查及重试结果满足预先定义的确认条件时，才能把对应故障标记为 `confirmed`；此时 `confirmation_source=rule`。人工确认时 `confirmation_source=human`。模型复核只能记录在 `review_source=model`，不能作为确认来源；只有状态为 `confirmed` 时才填写 `confirmation_source`，其他状态为空。超时本身不能直接确认为基础设施错误；向量库连接失败、外部业务 API 超时、模型服务超时和应用内部处理超时要按发生组件分别路由。证据不足保留 `unknown` 或 `inconclusive`。

大模型输出的 `confidence` 初期不参与路由，也不解释为校准概率。证据缺失时先运行确定性的证据补取步骤；检查确认来源中没有足够证据时，完成诊断并标记 `inconclusive`；若补取工具/模型调用执行失败，则标记 `retryable`，达到重试上限后为 `failed`，案例状态为 `blocked` 并等待人工处理。不能把执行失败记为 `inconclusive`，也不直接再次调用诊断 Agent 来掩盖证据获取失败。补证后仍有适用规则冲突、故障证据矛盾、故障类别无法确定，或属于高风险业务时，才按需调用复核。引用 ID 不存在或输出结构不完整走校验失败/定向重试，不当作“需要另一个模型猜测”。后续积累人工复核记录后，再按类别评估判断质量和校准阈值。意见冲突进入人工队列。验收要报告人工复核后的分类准确率、错误归因率、`unknown`/`inconclusive` 占比及各来源信号的命中分布；没有复核基线前不宣称归因准确。

## 5. 相似案例归并与多智能体分工

### 5.1 归并规则

- 只有用户意图、适用业务规则和答案依据一致时，才合并为同一个规范问题；关键词相似不构成合并依据。分类标签只用于分桶，不限制候选范围；允许跨桶召回相似候选，并对重复规范问题去重。
- “积分抵扣订单”和“积分提现现金”必须分开处理。政策、退款、账户等高风险主题优先保证正确性，采用更严格复核。
- 每个规范问题保存适用范围、权威依据及其版本、代表性问法、冲突/边界案例、归并理由和来源案例 ID。
- 在规定的数据保留期限内，通过稳定案例 ID 关联原始问法、Trace、检索证据和反馈。超过期限后按隐私策略删除或脱敏原始数据，只保留审计所需的非敏感处理记录；评估问题可有多个表达，不把每种表达都变成一条知识。
- 复核角色拒绝的组拆成独立候选，不能为了减少队列条目强行合并。

### 5.2 意图分类、主分析与条件复核

1. **主题/业务类别分类（RoBERTa）：** 复用已有多标签主题分类模型，不另加意图分类 Agent。输出标签、置信度和模型版本，用于分桶及候选召回优化；分类标签不代表故障类型，也不独立决定知识变更。分桶不能成为硬限制：归并候选允许跨桶搜索，并对重复规范问题去重。分类边界案例保留原始标签，可进入人工复核。
2. **主分析（大模型）：** 对小批案例一次调用，同时给出相似问题归并建议和初步故障归因，但将两类结果分别保存、分别校验。归并建议需引用案例 ID、共同主题/适用规则/答案依据；归因需引用 Trace 证据并给出故障类别或 `unknown`。每次实际模型请求记录后端生成的 `invocation_id` 和原始响应引用；一次调用可以关联一个归并逻辑任务和多个诊断逻辑任务，但它们各有独立 `task_id`、状态和结果（如 `merge_status`/`merge_result`、`diagnosis_status`/`diagnosis_result`）。若归并有效而诊断无效，只完成归并任务并定向重试诊断；重试生成新的 `invocation_id`，不得重复创建审核项、重复写入知识或覆盖另一分支已通过校验的结果。缺字段、解析失败、无效 ID 或证据引用只阻断对应逻辑任务。
3. **归并复核（大模型，条件触发）：** 仅在主分析提出合并候选时调用；核对意图、规则范围、答案依据和冲突。不一致或证据不足则拒绝合并并建议拆分。
4. **诊断复核（大模型，条件触发）：** 必要证据缺失时先检查能否从 Trace、日志、检索快照或工具记录补取；可补取则补齐后再诊断。检查确认来源中没有足够证据时，诊断可完成并标记 `inconclusive`；若补取调用执行失败，则将任务标为 `retryable`，耗尽重试后标为 `failed`、案例为 `blocked` 并转人工，不为同一份不完整证据再调用一个模型。只有补证后仍有归因冲突、故障类别不明或属于高风险业务时才调用诊断复核。初期不依据模型自报置信度触发；明确规则已确认的组件故障无需额外调用。
5. **人工审核：** 处理分类边界、复核拒绝、模型分歧、高风险主题和知识内容最终批准；按类别审核时仍可追溯到具体案例。
6. **确定性门禁：** 校验输入 ID、输出结构、证据引用、错误码、规则冲突和审核状态。模型自报置信度不能单独控制发布或绕过门禁。

RoBERTa is multilabel: report Micro-F1, Macro-F1, per-class Precision/Recall/F1/support, Hamming Loss (incorrect label cells divided by samples times labels), and Subset Accuracy (exact label-set match rate), with thresholds and per-class sample counts. The evaluator now emits these metrics; runtime acceptance still requires the trained ONNX artifacts.

主分析按批输出多个归并组，并为每个案例独立输出诊断；一个案例可以包含多个故障项。归并、诊断仍分别校验和持久化。下面只是接口形状示意，不是实际诊断结果：

```json
{
  "unmerged_case_ids": ["case_C"],
  "uncertain_case_ids": [],
  "no_merge_pairs": [],
  "uncertain_pairs": [
    {
      "case_id_a": "case_A",
      "case_id_b": "case_C",
      "reason": "insufficient_evidence"
    }
  ],
  "merge_groups": [
    {
      "canonical_question": "...",
      "member_case_ids": ["case_A", "case_B"],
      "rationale": "...",
      "rule_refs": ["knowledge:member-benefits#points"],
      "answer_basis_refs": ["knowledge:member-benefits#points"]
    }
  ],
  "case_diagnoses": [
    {
      "case_id": "case_A",
      "diagnosis_tasks": [
        {
          "scope": "answer_generation",
          "outcome_proposed": "faults_found",
          "faults": [
            {
              "type": "generation_error",
              "evidence_refs": ["trace:answer_A"],
              "rationale": "..."
            }
          ]
        },
        {
          "scope": "tool_call:shipping_api",
          "outcome_proposed": "inconclusive",
          "faults": [],
          "evidence_refs": ["trace:timeout_A"]
        }
      ]
    },
    {
      "case_id": "case_B",
      "diagnosis_tasks": [
        {
          "scope": "answer_generation",
          "outcome_proposed": "no_fault_found",
          "faults": []
        }
      ]
    },
    {
      "case_id": "case_C",
      "diagnosis_tasks": [
        {
          "scope": "answer_generation",
          "outcome_proposed": "no_fault_found",
          "faults": []
        }
      ]
    }
  ]
}
```

结构中不要求主分析返回一个整体 `confidence` 来代表多个任务。若保留模型自评分，它只能作为诊断元数据，初期不用于路由。模型只输出语义结论和引用：不生成 `batch_id`、`task_id`、`canonical_question_id` 或 `group_result_id`，也不决定任务执行状态或确认来源。后端生成并持久化这些 ID；`case_id` 由后端提供给模型引用。`member_case_ids`、`unmerged_case_ids` 和 `uncertain_case_ids` 是案例级分桶，后端校验它们互不重复且完整覆盖本批：`unmerged_case_ids` 表示案例本身可分析但没有进入已提议分组；`uncertain_case_ids` 只表示案例因关键数据缺失等全局原因不能可靠参与分析。案例对的不确定性单独通过 `uncertain_pairs` 表示，因此一个案例可以属于归并组，同时与组外另一案例构成不确定关系。每个 pair 记录不可变的 `relation_proposed`、表示原提议是否被接受的 `review_status`（`pending`、`confirmed`、`rejected`），以及独立的 `relation_final` 和 `confirmation_source`。`review_status=confirmed` 表示原提议被接受，`rejected` 表示原提议被推翻；审核状态不决定 `relation_final`。例如模型提出 `merge` 被人工驳回，但审核证据确认是不同规范问题时，允许写入 `relation_final=no_merge`、`review_status=rejected`、`confirmation_source=human`；模型提出 `no_merge` 被驳回但确认应合并时，最终写 `merge`。只有规则或人工确认的明确关系才可写入 `relation_final=merge/no_merge`；审核完成仍无法判断时写 `relation_final=uncertain`。`review_source=model` 只是复核意见，不能作为确认来源。`no_merge_pairs` 和 `uncertain_pairs` 均按无序 pair 保存；后端校验两端 ID 属于本批、规范化排序并去重，同一 pair 不能同时落入同一归并组、`no_merge_pairs` 和 `uncertain_pairs`。不同组之间默认未判定；仅在审核确认对应 `canonical_question_id` 不同后，才形成最终负例。后端为每个固定成员集合分配 `group_result_id`，不承担规范问题的长期身份；同一规范问题跨批次和成员变化时继续复用 `canonical_question_id`。若人工确认需要拆分，则创建不同规范问题 ID，并保留 `split_from` 等谱系记录。每个归并组需提供共同规则与答案依据引用；引用缺失或无效时可以保留候选组，但由后端设置 `evidence_status=needs_verification`，不得据此生成知识写入。

诊断分开保存**任务执行状态**与**诊断结论**。Workflow 管理每个诊断子任务的 `execution_status`（`pending`、`running`、`completed`、`retryable`、`failed`）；模型只提出 `outcome_proposed` 和故障项；只有诊断执行成功完成后，Workflow 才生成该子任务的 `outcome_final`（`faults_found`、`no_fault_found` 或 `inconclusive`）。`outcome_final=faults_found` 表示至少一个故障已确认且该子任务没有未处置的故障提议；`no_fault_found` 表示规定检查已完成、无故障确认、该范围内的所有故障提议都已被规则或人工合理驳回且无未解决不确定性；`inconclusive` 表示检查已完成但证据不足/冲突或仍有未处置故障提议。若执行解析失败、模型调用失败或必要检查未完成，`outcome_final` 保持为空；重试耗尽后 `execution_status=failed`，不能把执行失败记作 `inconclusive`。案例级汇总使用独立字段 `case_diagnosis_status`，取值为 `faults_found`、`no_fault_found`、`partially_diagnosed`、`in_progress`、`inconclusive` 或 `blocked`。汇总规则：任一必需子任务在重试耗尽后为 `failed`，案例为 `blocked` 并等待人工处理，已确认的其他故障仍保留在子任务记录中；否则仍有子任务待执行、运行或等待重试时，无已确认故障则为 `in_progress`，已有确认故障则为 `partially_diagnosed`；所有必需子任务均成功完成且至少一项故障确认、无未解决不确定性或未处置提议时为 `faults_found`；全部完成、没有确认故障、所有故障提议均已被规则或人工合理驳回且无未解决不确定性时为 `no_fault_found`；全部完成但仍有未处置故障提议或其他无法可靠判断事项时，无已确认故障为 `inconclusive`，已有确认故障为 `partially_diagnosed`。未确认的故障提议不能自动当成“无故障”；规则无法处置时进入人工审核，审核/诊断流程完成仍无法判断才记为 `inconclusive`。`case_diagnosis_status` 由 Workflow 派生，不由模型决定。漏项或重复项标记对应子任务不完整。同一批可以有多个归并组；同一案例可有多个独立故障项。结构化响应解析后逐项校验并持久化；单项无效只阻断该项。整份响应无法解析时保留已持久化结果，只将未完成任务标为可重试。后端按任务类型、输入指纹、分析配置版本和输出 Schema 版本生成/复用稳定 `task_id`；同版本重试复用 ID，更换模型/提示词/参数或 Schema 后创建新任务记录。

| 标识/字段 | 生成与校验职责 |
|---|---|
| `batch_id` | 后端生成，标识一次批次处理 |
| `case_id` | 后端为原始案例生成并传给模型；模型只能引用，后端校验归属本批 |
| `invocation_id` | 后端为每次实际模型请求生成，关联模型配置、原始响应及其解析结果；原始响应按隐私策略保护和限权；一次调用可供多个逻辑任务使用 |
| `task_id` | 后端基于任务类型、输入指纹、分析配置版本和输出 Schema 版本生成并持久化；同版本重试复用，更换分析配置或 Schema 则新建 |
| `canonical_question_id` | 后端维护的规范问题长期身份；同一语义问题跨批次和成员变化时复用，人工拆分时创建新 ID 并保留谱系 |
| `merge_task_id` | `task_type=merge` 时，`merge_task_id` 就是通用 `task_id` 的领域别名，两者值相同、共用一条任务记录；不另建父级任务 ID。同一归并任务可产出多个分组 |
| `group_result_id` | 后端为固定成员集合的归并结果记录生成；幂等键为 `merge_task_id + 排序后的 member_case_ids`。相同成员关系重试复用该 ID；成员集合变化创建新的 `group_result_id` 并保留旧记录 |
| `result_revision` | 同一 `group_result_id` 下的内容修订序号。相同成员关系重试若归并理由或证据引用发生实质变化，追加新修订并保留前一内容；审核通过、拒绝或重新审核单独追加审核事件，不改写结果内容或修订号 |
| `member_case_ids` | 大模型提出，后端校验其均属于本批且满足归并约束；成员变化时创建新的 `group_result_id`，保留旧审核记录，不原地覆盖 |
| `no_merge_pairs` | 大模型提出的明确 pair 级不合并判断，初始仅是提议，须经审核/规则确认后才作为最终判断；后端校验两个 case ID 均属于本批且成对状态无冲突。未进入同一组或列在 `unmerged_case_ids` 本身不自动等同于此结论 |
| `uncertain_pairs` | 可选的 pair 级弃权关系；后端校验两端 case ID 均属于本批，以排序后的 ID 规范化为无序 pair 并去重；需与归并组内 pair 和 `no_merge_pairs` 互斥，并保留原因 |
| `relation_proposed` / `review_status` / `relation_final` | `relation_proposed` 不可变；`review_status` 仅表示原提议是否接受（`pending`、`confirmed`、`rejected`）；`relation_final` 独立保存 `merge`、`no_merge` 或 `uncertain`。拒绝原始 `merge` 提议后可以确认最终 `no_merge`，反之亦然 |
| `review_source` / `confirmation_source` | 复核来源可记录 `model` 或 `human`；最终明确关系的确认来源仅为 `rule` 或 `human`。模型复核意见不能单独确认最终关系；最终为 `uncertain` 时确认来源为空 |

`analysis_config_version` 应能追溯供应商/模型版本、角色提示词版本或哈希、生成参数及相关解析配置；它与 `output_schema_version` 一起进入任务幂等键。同一 `merge_task_id`（即该归并任务的 `task_id`）可产生多个 `group_result_id`，每个分组结果幂等键按成员列表分别计算，不让多个分组共享同一个组级键。`canonical_question_id` 由后端结合已有规范问题目录和审核后的语义关系解析，不由模型生成，也不从临时成员列表直接推导。已持久化结果不可被重试覆盖；内容实质变化时追加 `result_revision`，并保留旧内容、审核结论和变更原因。

| 归并结果 | 归因结果 | 处理方式 |
|---|---|---|
| 正确 | 正确 | 进入对应候选与评估流程 |
| 正确 | 错误/待定 | 保留归并结果，只复核或重跑诊断 |
| 错误/待定 | 正确 | 保留诊断结果，只复核或重跑归并 |
| 不确定 | 不确定 | 进入人工审核；证据暂缺时标记 `inconclusive` 并允许后续重试 |

诊断状态的职责汇总如下，模型输出只包含提议字段；执行状态、最终结论及确认状态由 Workflow 持久化维护：

| 字段 | 管理方 | 含义 |
|---|---|---|
| `execution_status` | Workflow | `pending`、`running`、`completed`、`retryable`、`failed`；表示执行进度，不代表诊断结论 |
| `outcome_proposed` | 大模型 | 初步诊断结论，只是待验证提议 |
| `outcome_final` | Workflow | 仅在该诊断子任务成功完成后生成；执行失败或未完成时为空，执行完成但无法可靠判断时才为 `inconclusive` |
| `case_diagnosis_status` | Workflow | 案例级汇总字段；包括 `faults_found`、`no_fault_found`、`partially_diagnosed`、`in_progress`、`inconclusive`、`blocked`，不与子任务 `outcome_final` 混用 |
| `faults[].status` | Workflow/审核流程 | 每项故障独立为 `proposed`、`confirmed` 或 `rejected` |
| `proposal_source` | 提议方 | 故障提议来源，如 `model` 或 `rule` |
| `review_source` | 复核方 | 复核意见来源，如 `model` 或 `human`；模型复核只是意见 |
| `confirmation_source` | Workflow/审核流程 | 仅在故障确认为 `confirmed` 时记录 `rule` 或 `human`；未确认时为空 |

Workflow 控制节点顺序、条件分支、重试和状态持久化；Agent 只处理语义归并、复杂归因及按需复核，不负责发布控制。若采用 LangGraph，可将 `collect_cases`、`classify_topic`、`check_infrastructure_error`、`route_by_fault_type`、`run_evaluation`、`publish_gate` 作为确定性节点；`analyze_and_merge`、`review_merge`、`review_diagnosis` 作为模型节点。State 保存案例 ID、RoBERTa 主题/业务类别标签、每批多个归并组、逐案例故障项列表、独立任务状态、证据引用、受影响分支状态、候选及评估结果，条件边根据证据与风险路由。仅当现有代码无法可靠管理持久状态、条件分支、失败恢复和人工审核时，才考虑引入 LangGraph；当前这是目标编排设计，不表示项目已接入 LangGraph。

主分析大模型一次完成归并建议和初步归因；复核模型按条件触发，而不是每条案例固定串联多个 Agent。归并与归因分别统计正确率、错误合并率、故障分类准确率和未知比例；当一项表现不足时，只拆分或重跑该项。RoBERTa 分类单独运行，不占用大模型调用。分别记录分类推理成本、主分析及各复核分支调用次数与 Token、人工耗时和各分支错误率，再决定是否调整流程。每批最多 10 条。为避免重复调用和数据丢失：案例、批次、规范问题和候选都使用稳定 ID；每个案例的分类、归并、归因及复核任务各自使用稳定 `task_id`，重试复用同一 ID；任务状态和结果分别持久化，重复提交幂等；部分成功可从未完成任务恢复；设置最大重试次数，超过上限进入可查询的失败队列并保留错误原因。重试不得重复创建审核项或知识写入。

归并验收统一以**无序案例对**作为统计单位。模型原始提议和审核最终关系分开保存：同一模型提议归并组内的 pair 是 `relation_proposed=merge`；`no_merge_pairs` 是 `relation_proposed=no_merge`；`uncertain_pairs` 是模型弃权。`relation_proposed` 不可变。`review_status` 只表示原始提议是否被审核接受（`pending`、`confirmed`、`rejected`），不表示最终关系取值；`relation_final` 独立记录审核后的 `merge`、`no_merge` 或 `uncertain`。因此，模型提议 `merge` 被拒绝后，若审核确认两个案例属于不同规范问题，可以记录 `review_status=rejected`、`relation_final=no_merge`、`confirmation_source=human`；模型提议 `no_merge` 被拒绝后，若确认应合并，则记录 `relation_final=merge`。只有规则或人工证据确认时才能写入明确的 `relation_final=merge/no_merge`；审核完成仍不能判断时写入 `uncertain`。`review_source=model` 只能记录复核意见，不能单独确认最终关系。两个不同组之间默认未判定，只有审核确认对应 `canonical_question_id` 不同后才形成最终负例。全局 `uncertain_case_ids` 涉及的 pair 若无独立明确结论也按弃权处理。所有 pair 按无序 ID 规范化、去重；同一 pair 不得同时属于归并组内合并、`no_merge_pairs` 和 `uncertain_pairs`。自动模型 Precision/Recall 只用不可变的 `relation_proposed` 对照人工标注计算，不能让审核修改掩盖原模型错误；自动召回中弃权或漏提出的真实正例仍计漏检。审核后的指标单独使用 `relation_final`，并分别报告明确关系质量、最终弃权率、审核覆盖率、人工改判率和模型弃权比例。分组级质量如需报告，单独标记，不能与 pair 级指标混用。

按风险、来源和抽样层（同桶、跨桶近邻、随机对）分别报告原始计数与指标：

- **自动合并精确率：** 人工标注为应合并的模型原始合并提议对 / 模型原始合并提议对；不使用人工审核后的修改结果替代模型原始输出。
- **自动合并召回率：** 人工标注应合并且模型原始输出已提出合并的案例对 / 审计范围内人工标注应合并的案例对；弃权与漏提出的真实正例均计为未命中。
- **漏归并率：** 审计范围内人工标注应合并但模型原始输出未提出合并的案例对 / 审计范围内人工标注应合并的案例对；与同口径自动合并召回率互补。
- **错误合并比例：** 模型原始合并提议中人工判定不应合并的比例；在相同提议集合分母下，它等于 `1 - 自动合并精确率`，报告时注明分母。
- **审核后关系准确率：** 分子为 `relation_final` 与人工金标准一致的明确关系数；分母只含 `relation_final=merge` 或 `no_merge` 的 pair，不含 `uncertain`。必须同时报告分子、分母及最终弃权率，避免选择性排除弃权造成误读。
- **最终弃权率：** `relation_final=uncertain` 的已审核 pair 数 / 所有已完成审核并有 `relation_final` 的 pair 数。模型原始弃权率另行统计，不能与审核后弃权率混用。
- **审核覆盖率：** 已完成审核且写入 `relation_final` 的 pair 数 / 规定审核范围内的 pair 总数；分层抽样时保留原抽样权重。
- **人工改判率：** 分别报告审核推翻模型合并提议、推翻模型不合并提议的比例；模型原始提议不可被审核结果覆盖。

每层分别报告抽样数量和抽样概率。若要把分层结果汇总估计到定义好的审计范围，应按纳入概率进行加权；不加权时明确结果仅代表审计样本，不推广到全部线上案例。随机抽样对和近邻对的统计也不能宣称覆盖所有可能的问题对。每次抽查保留标注者、案例对、理由和分歧裁决。至少保留被拒绝、被拆分及漏归并的样例。对高风险规则执行更严格的人工确认；若错误合并代价大，不以减少审核量作为优化目标。

故障诊断的统计单位应明确为“案例 × 诊断范围/故障项”标注关系；多标签案例不能压成单一类别。报告各故障类别的 Precision、Recall、F1 和支持样本数，并报告错误归因率、`unknown`/`inconclusive` 比例、案例级 `partially_diagnosed` 比例及执行终止失败的 `blocked` 比例，同时给出分子、分母与统计范围。模型提议项与规则/人工确认项分开统计，避免把未确认提议当成真实故障标签。

## 6. 知识版本、实验快照与回滚

### 6.1 数据分层

- **案例库：** 线上问题、错误回答、证据和审核记录，用于诊断与归并，不参与在线问答检索。
- **评估集：** 问题、预期处理、必需证据及来源，用于回归验证，不作为知识源。
- **正式知识库：** 经审核的业务规则和产品事实，作为 RAG 检索依据。

AI 草稿、点踩问题和模型原始回答不得直接写入正式知识库。确认是知识缺口后，依据权威来源形成规范知识，附常见问法或别名，经批准后进入新版本。

### 6.2 三层版本边界

知识资产、运行配置和一次实验/发布是三个独立版本对象。知识更新不应被强制视为系统配置更新，系统参数变化也不应伪装成知识快照变化。

| 对象 | ID 示例 | 记录内容 |
|---|---|---|
| Knowledge/config/release snapshots and compatible rollback | Code now includes immutable snapshot/config/release catalogs, Milvus active-alias switching, compatibility checks, and rollback commands; migrations and live rollback acceptance remain pending |
| 系统配置版本 | `system_config_version` | 应用代码提交、提示词、聊天模型、重排模型、检索参数、查询嵌入模型及预处理配置 |
| 实验/发布记录 | `run_id` / `release_id` | 实际引用的知识快照与系统配置组合、数据集指纹、评估结果、审批、发布时间及当前生效状态 |

这些对象各自不可变。一次实验/发布记录绑定精确组合，便于复现并独立调整知识和系统配置。嵌入模型同时涉及离线索引构建和在线查询向量生成：知识快照记录构建索引所用的模型，系统配置记录查询所用的模型；两者的供应商、模型版本、向量维度和预处理必须兼容。

发布记录本身不可变；当前生产环境指向哪个 `release_id` 由单独的运行时指针/部署状态记录维护，并保留切换审计，不能通过修改旧发布记录来表示回滚。

### 6.3 回滚与兼容性门禁

- 仅修改知识内容且索引构建与查询侧嵌入配置兼容时，可以只切换知识索引。
- 若嵌入模型、向量维度、分块/预处理或其他影响检索兼容性的配置变化，必须恢复与目标知识快照兼容的完整运行组合，不能只切换 Milvus 集合。
- 切换前校验知识快照与系统配置的兼容清单，包括索引嵌入模型、查询嵌入模型、维度、预处理、集合 Schema 和检索参数；不匹配时禁止切换。
- 切换后验证实际查询向量生成、集合读取、检索/重排输出及配置 ID，再运行固定冒烟查询和现有回归集。
- 保留当前生效和上一稳定发布组合。只保存文档哈希、版本名或只切换索引而不核对查询向量兼容性，不算可回滚。

若无法保留旧索引，应能从不可变知识材料及对应构建环境重建。嵌入模型、分块规则或索引方式改变时，应登记为新的知识快照，不声称与旧向量索引逐字节相同。

## 7. 离线评估与实验可信度

现有 18 条回归集和 30 条挑战集继续用于已知问题回归与项目验收。它们已参与问题扩展和修复，反复调优可能过拟合；因此每次实验都留档，不能只保留最好的结果。报告总体结果、每个类别的样本数和指标、具体失败样例及候选去留理由。未使用独立数据验证时，独立泛化结论标为 `inconclusive`。

### 7.1 指标定义与检索阶段

- 标准文档级 **Recall@K** 定义为：对问题 `q`，`|R_q ∩ T_q^(K)| / |R_q|`，其中 `R_q` 是全部标注相关文档，`T_q^(K)` 是前 K 个返回文档。若每题只有一个相关来源，二值命中更适合称 **Hit Rate@K**，或明确其为单相关来源特例。
- **当前项目实现并非文档级 Recall@5。** `scripts/eval_ch04.py` 将 `expect_section` 中的名称作为同一证据组的别名；`expect_sections_all` 中每一组表示一个必需证据组、组内为别名。系统取每组最早命中的 section rank，`_recall_at` 计算前 K 命中的证据组比例，再对题目/类别求平均。因此当前字段 `recall_at_5` 应在报告中标为 **section-group Recall@5（证据组召回率）**，不能笼统称为标准文档 Recall@5。原始命中组数、总组数、题目数和分母要同时报告。
- 当前字段 `mrr` 对每题的每个证据组分别取最早命中排名的倒数，漏组记 0，先对题内证据组求平均，再对题目宏平均。它是 **section-group MRR**；单证据组时与标准 MRR 一致，多证据组时不是“第一个相关文档”的经典 MRR。报告名和定义需明确这一点。
Evaluation separates retrieval stages: the pre-rerank candidate list supplies section-group Recall@20, while the final reranked list supplies section-group Recall@5 and MRR. The candidate evaluator now records both lists and reports pre-rerank Recall@20 alongside the final ranking metrics, enabling retrieval-versus-reranking attribution.
- 已核对实际代码：`app/core/optimization_candidates.py` 的 `recall_20` 候选把 `recall_top_k` 设为 20；`app/config.py` 默认 `recall_top_k=50`，`rerank_top_k=10`。所以 `recall_20` 是候选配置名，不是 Recall@20 指标。配置值仍应由评估报告记录，不要仅凭名称推断运行行为。
- 同时报告回答覆盖、忠实度、拒答/转人工合规、工具成功率、模型调用数、Token、延迟和人工审核时间。LLM Judge 需抽样与人工判断对照，报告一致率及分歧案例。

因此，`recall_20`（配置名）、重排前 section-group Recall@20（指标）和重排后 section-group Recall@5/MRR（指标）是三个不同概念，报告需显式区分。

调整 `top_k`、重排器或嵌入模型前，先验证权威文档是否存在、分块是否正确、文档是否进入当前有效索引、索引版本是否匹配，以及元数据/权限过滤是否误排除。只有索引完整性确认后，才把问题归为检索参数问题并做对照实验。

### 7.2 单变量对照和留档

| 实验 | 固定条件 | 唯一主要变化项 |
|---|---|---|
| 检索参数优化 | 基线知识快照、基线系统配置、评估集 | 新建候选 `system_config_version`，只修改目标检索参数并记录差异 |
| 知识内容更新 | 基线系统配置、分块/预处理/嵌入及索引构建方案、评估集 | 新建候选 `knowledge_snapshot_id`，仅修改经过审核的知识内容 |
| 索引构建方案更新 | 知识文档内容、评估集及未涉及的配置 | 新建候选 `knowledge_snapshot_id`，单独修改分块、索引预处理或构建方案；若更改嵌入模型，必须同步创建兼容的查询侧 `system_config_version`，并将嵌入组合明确作为实验变量 |
| 回答策略优化 | 基线知识快照、基线系统配置、评估集 | 新建候选 `system_config_version`，只修改目标提示词/回答策略并记录差异 |

知识内容实验固定分块、预处理、嵌入模型和索引构建方案；索引构建实验固定文档内容并明示改变了哪项构建因素。若同一实验同时改知识内容与构建方案，应设计能区分两者的对照，或明确标注为组合效果，不把收益只归因于知识内容。基线版本和每个候选版本均不可变；修改目标参数就创建新的 `system_config_version`，保留与基线的字段级差异，不得在同一个 ID 下原地修改。每次运行生成 `run_id`，记录时间、操作者、数据集哈希、`knowledge_snapshot_id`、候选 `system_config_version`、完整指标、失败题、成本以及最终是否采用；发布生成 `release_id` 并绑定实际运行组合。失败运行和被淘汰候选也要记录。生成模型存在随机性时固定采样参数，必要时重复运行并报告波动。发布门槛应包含总体和关键类别最低标准、不可回退项及样本量；18 条回归集和 30 条挑战集较小，必须同时报告原始命中/失败数和分母，不得仅凭少数题造成的微小分数变化宣称成功。

## 8. 成本、抽样与隐私

- 先用确定性规则筛除明确工具错误和重复案例；小批归并，每批最多 10 条；只对拟合并组调用复核角色。
- RoBERTa 标签用于候选分桶但不硬性截断跨桶搜索；定期检查跨桶召回与规范问题重复率，避免分类错误造成漏归并。
- 按类别审核和风险升级减少重复逐条审阅，同时保留案例级追溯与改判能力。
- 每轮记录归并/复核调用数、Token、失败与重试次数、人工审核量和耗时、错误合并率及被拆分数量，比较节省的人工时间与新增模型成本。
- 线上问题样本同时按流量和风险分层抽取，统计点踩、低置信度、工具失败、抽样发现等来源占比，评估反馈机制覆盖偏差。
- Trace 仅采集复现与诊断所需字段；用户原文、订单信息、工具参数和模型输入按字段脱敏或最小化。设置角色访问权限、保留期限、到期删除和读取/导出审计，不为方便复现无限期保存原始用户数据。
- 用户原文、Trace、日志、工具返回值和检索到的知识片段一律作为待分析的不可信数据，不作为可执行指令；分析 Agent 仅获得完成任务所需的最小读取权限。Agent 不直接拥有生产知识库写入、生产发布或系统配置修改权限，输出由后端做 Schema、ID、证据及权限校验，并继续经过人工审批与发布门禁。

## 9. 实施优先级与验收

| 顺序 | 优先级 | 工作项 | 验收标准 |
|---|---|---|---|
| 1 | P0 | 补齐诊断证据链、组件级故障确认规则及先补证后复核的分支 | 单次超时不直接确认为基础设施故障；记录故障组件/依赖/健康检查/重试；证据源核查完成但仍不足时为 `completed` / `inconclusive`；补取执行失败重试耗尽时为 `failed` / `blocked`；基础设施异常只暂停受影响分支 |
| 2 | P0 | 完善批量输出、后端 ID 和任务级持久化；明确诊断确认来源 | 一批可输出多个归并组；长期 `canonical_question_id` 与固定成员集合的 `group_result_id` 分离；成员变化创建新结果记录，规范问题跨批次复用 ID；`merge_task_id` 与 merge 类型的 `task_id` 为同一标识；任务键包含输入和分析/Schema 版本；理由/证据变化追加 `result_revision`，审核状态另记事件；模型不合并提议与最终关系分开保存；每次模型请求有 `invocation_id`，归并/诊断逻辑任务可独立恢复；诊断分别保存 `execution_status`、`outcome_proposed`、`outcome_final` 和故障项状态，案例使用 `case_diagnosis_status` 汇总，终止执行失败进入 `blocked`；部分失败恢复不覆盖已通过结果或重复写入 |
| 3 | P0 | 拆分知识快照、系统配置和发布记录；实现可切换/可重建知识快照及兼容性回滚验证 | 指定组合可恢复；索引与查询嵌入兼容性通过门禁；回滚后冒烟及回归通过；实验/发布绑定完整组合 |
| 4 | P1 | 验收归并漏检与 RoBERTa 多标签分类 | 分开案例级未分组/全局弃权和 pair 级 `no_merge_pairs` / `uncertain_pairs`；涉及弃权的 pair 不自动算负例，但人工标为正例时仍计入自动召回漏检；分别报告模型原始合并 Precision/Recall、审核后明确关系准确率（分母仅含 merge/no_merge）、最终弃权率、审核覆盖率和人工改判率；按无序案例对和抽样层报告；分类报告 Micro/Macro-F1、逐类 P/R/F1、Hamming Loss、Subset Accuracy、样本数及阈值 |
| 5 | P1 | 核验评估指标实现、补充重排前召回指标、区分知识内容与索引构建实验 | 报告区分标准文档级指标与 section-group Recall/MRR、`recall_20` 配置名和重排前后阶段；知识内容实验固定构建方案，索引构建实验固定文档内容；呈现原始分子/分母且失败实验可追溯 |
| 6 | P1 | 验证分批归并、选择性复核的实际收益并加强线上抽样/隐私治理 | 提供实际评估输出样例，核对指标与分母；对归并任务做失败恢复/重复提交验收；执行真实知识索引版本切换和匹配配置回滚并通过冒烟、回归；用真实运行数据比较成本、人工时间、错误合并率和遗漏率；验证不可信输入处理、最小权限、保留期限及删除审计 |
| 7 | 发布门禁 | 人工批准候选并灰度发布 | 每次发布均生成 `release_id` 并绑定实际知识/配置版本；发布前门禁通过；保留上一稳定组合；发布后验证，异常能恢复兼容组合并复验 |

## 10. 完成标准

- 同类问法能归并展示，不同业务规则不会仅因词面相似而合并；错误合并可发现、拆分和追溯。
- 每批可表示多个归并组，每个案例的诊断可包含零个或多个独立故障项；单项失败和重试不会覆盖其他已完成结果。
- `canonical_question_id` 跨批次标识同一规范问题；`group_result_id` 标识固定成员集合的归并结果记录，按 `merge_task_id` 与排序成员形成幂等键。成员变化产生新结果记录；理由或证据变化追加 `result_revision`；审核状态变化单独记审核事件，均不能静默覆盖历史。人工拆分保留 ID 谱系。任务键同时绑定输入指纹、分析配置版本和输出 Schema 版本。
- 模型输出区分案例级未分组/全局弃权与 pair 级 `no_merge_pairs` / `uncertain_pairs`；pair 无序去重且状态互斥。`review_status` 只表示原提议是否接受，`relation_final` 独立记录审核关系；被拒绝的 merge 提议在证据确认不同规范问题后可以得到最终 `no_merge`。自动模型指标与审核后最终明确关系准确率分开报告，另报最终弃权率和审核覆盖率。归并组携带规则/答案依据引用。诊断分开保存执行状态、提议结论、任务级最终结论和逐故障项状态；执行终止失败时案例为 `blocked`，不得写成 `inconclusive`；存在未处置故障提议时不能判为 `no_fault_found`；`case_diagnosis_status` 明确区分 `faults_found`、`no_fault_found`、`partially_diagnosed`、`in_progress`、`inconclusive`、`blocked`；报告各故障类 P/R/F1、错误归因、未知、部分诊断及 blocked 比例，并给出统计单位和分母。
- 每次模型请求均记录 `invocation_id`；一次请求中归并有效而诊断失败时，归并任务保留已验证结果，只定向重试诊断分支，且不重复创建审核项或写入知识。
- 每条诊断、归并、候选、评估和发布都能追溯到原始案例、证据、审批者及具体知识快照、系统配置和发布组合；原始数据关联遵循保留期限，过期后删除/脱敏并保留必要的非敏感审计记录。
- 证据不足时允许 `unknown` 或 `inconclusive`，并能只重试受影响的任务分支；任务级稳定 ID 保证重试不覆盖另一任务结果，也不重复创建审核项或候选。
- 回滚前验证知识索引与查询嵌入、向量维度、预处理、集合 Schema 和运行配置兼容；回滚后通过冒烟与回归验证。
- 检索报告准确区分标准文档级 Recall、当前自定义 section-group Recall/MRR，以及配置名称 `recall_20`；重排前后指标和题目/证据组分母均可查看。
- 评估明确标记现有数据集的用途边界，记录每次尝试，知识、检索参数和回答策略按单变量对照。
- 指定知识版本可以真实恢复或切换，且恢复后通过查询验证；保留上一稳定版本。
- 报告同时呈现质量、类别失败、模型成本、人工工时和线上问题发现来源；未经独立验证不宣称独立测试通过。
- 验收留存三类可复核证据：实际评估脚本输出样例；归并任务失败恢复和重复提交结果；实际知识索引切换、兼容配置回滚及回归验证记录。分析 Agent 将用户/Trace/日志/工具和知识内容作为不可信数据，使用最小权限且无法直接修改生产资产。
- 未审核的答案草稿不会进入正式知识库；所有发布均经人工批准和评估门禁。

## 11. Implementation status (2026-10-26)

本轮已在现有低置信度问题飞轮中落地第一批工程能力：归并与复核逻辑任务按输入指纹、模型/提示词配置哈希和输出 Schema 版本生成稳定 `task_id`；每次真实模型请求记录 `invocation_id`、状态、响应哈希和错误类型；已通过校验的结构化任务结果可在重试时复用，结果正文 30 天后清除；待审核队列创建、出现次数递增、来源案例关联及诊断案例回链在同一数据库事务内按批次提交，避免批次中途失败造成重复计数或半批落库。重试上限为 3 次，超过后标记 `failed` 并保留在任务表中供查询。迁移文件为 `sql/ch12-flywheel-execution.sql`。

Additional code now implements RoBERTa topic classification plus evidence-referenced LLM diagnosis proposals, persisted tasks/invocations, pair-relation persistence and human review API/page, audited-sample metrics, knowledge snapshots, immutable config/release records, active Milvus alias switching, and compatible rollback.

Docker was started and migrations ch12 through ch16 were applied to the local database. The API is reachable at `http://127.0.0.1:8000/docs`; MySQL and Milvus are healthy. A live flywheel run exposed two malformed historical inputs (`行` and `2`), so a deterministic intake gate and `pending`/`queued`/`ignored` source status were added. The original cases remain stored as `ignored`; the review items created by that run were removed from the active queue while their canonical and pair/group audit records were retained. A subsequent flywheel run created no new queue items. The RoBERTa ONNX model/tokenizer/threshold artifacts are still missing. Retrieval evaluation was attempted but returned `incomplete` because the SiliconFlow embedding/rerank account has insufficient balance; this is not a passing evaluation. Pair metrics cover human-reviewed samples only; population merge recall still requires stratified audits of unproposed same-bucket, cross-bucket-neighbor, and random pairs with sampling probabilities recorded. Snapshot switching and compatible rollback have not yet been exercised. Current status: infrastructure and migrations are running; some recovery/intake behavior is exercised, but end-to-end effectiveness and release rollback remain unverified.
