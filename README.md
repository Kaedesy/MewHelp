# MewHelp — 电商智能客服 Agent

《AI Agent 智能客服实战》的配套源码。一个能查订单物流、答政策 FAQ、走退款子流程、
挖知识补库、还能微调一个主题分类器的完整客服系统。

代码是随课程一章章长出来的，不分支：ch01 一个纯对话接口起步，到 ch10 收尾时是下面这套东西。

## 技术栈

FastAPI + LangGraph / LangChain + SQLAlchemy / MySQL + Milvus。

聊天、嵌入、重排三组上游各自直连，没有网关那一层。模型名和地址都在 `.env` 里配
(`CHAT_*` / `EMBED_*` / `RERANK_*` 三组)，换供应商、换模型不用改代码。

## 跑起来

```bash
cp .env.example .env      # 填 CHAT_* / EMBED_* / RERANK_* 三组
docker compose up -d      # MySQL
make seed                 # 灌业务测试数据
make dev                  # 依赖容器 + MCP :8101/:8102 + 应用 :8000
```

浏览器打开 <http://localhost:8000> 就是聊天页。

**详细的安装、配置、建知识库、常见问题，看飞书那篇「MewHelp 项目源码下载」**，
这里只留一条能把服务拉起来的最短路径。两边写岔了以那篇为准。

## 代码怎么组织

| 位置 | 装的是什么 |
| - | - |
| `app/api/` | HTTP 入口。聊天、Agent、知识库录入、复核、验收页、成本看板 |
| `app/graph/` | LangGraph 那张图。`state` 状态、`nodes` 节点、`routing` 分流规则、`build` 组装 |
| `app/core/` | 单点能力。上游客户端、检索、重排、意图、指代、摘要、置信度、飞轮、可观测 |
| `app/kb/` | 知识怎么进库。切块、嵌入、双写 MySQL 与 Milvus、去重、从对话里挖问答对 |
| `app/tools/` | 工具系统。内置 `@tool`、MCP 客户端、注册表、统一执行引擎 |
| `app/db/` | 表模型与仓储 |
| `app/static/` | 前端页面。聊天、知识库录入、飞轮待审、观测与成本、主题分布、分类器验收 |
| `mcp_servers/` | 两台业务 MCP Server，物流和售后各一台，独立进程 |
| `sql/` | 各章的建表与迁移，容器首启按文件名顺序自动执行 |
| `scripts/` | 建库、评估、微调这些离线活 |
| `docs/superpowers/` | 各章的 spec 和 plan。课程实战篇教的就是这套流程，留着当范本 |

## 各章长出了什么，怎么验

`make test` 跑全部单测，不打真实模型。下面这些要真服务在跑。

| 章 | 这一章长出来的东西 | 验收 |
| - | - | - |
| ch01 | 流式对话、结构化提取 | `make eval` |
| ch02 | 五个 `@tool` 业务工具，单轮 Function Calling | `make eval-agent` |
| ch03 | 切块、嵌入、MySQL 与 Milvus 双写、对话挖知识 | `make kb-build` `make kb-vectorize` `make eval-retrieval` |
| ch04 | 混合检索、RRF、重排、Query 改写、四策略评估 | `make smoke-rag` `make eval-rag` |
| ch05 | LangGraph workflow 骨架 + 主力 Agent 的 ReAct 环 | `make eval-ch05` |
| ch06 | 分流器、指代消解、退款子流程的 interrupt/resume | `make smoke-interrupt` `make eval-ch06` |
| ch07 | 上下文管理。滑窗、摘要、前缀缓存 | `make eval-ch07` |
| ch08 | 工具系统。MCP 动态发现、统一执行引擎、审计日志 | `make eval-ch08` |
| ch09 | Langfuse 自部署、数据飞轮、成本账 | `make langfuse-up` `make flywheel` `make eval-flywheel` `make cost-report` |
| ch10 | 主题分类器。语料、微调、阈值扫描、ONNX 推理服务 | `make ch10-corpus` `make ch10-train` `make ch10-eval` |
| ch11 | 失败案例诊断、检索候选离线对比、回归门禁 | `make eval-candidates DATASET=... DATASET_ROLE=validation` `make release-gate CANDIDATE=...` |

每条命令的前置条件（哪些服务得先起、哪张表得先建）写在飞书那篇文档的「各章验收」段里。
`make help` 也能看到带说明的完整目标清单。

### 增量优化实验（ch11）

失败案例带应用级 `case_id`，可在 Langfuse Trace metadata 中搜索；低置信拒答、失败工具和用户点踩会进入诊断记录，待人工确认。只有人工确认的 `knowledge_gap` 会幂等写入低置信问题池，继续经过现有归一化查重、`review_queue` 人工审核和批准后 `dualwrite` 写回流程；审核详情保留诊断案例链接。其它诊断类别不会自动转成知识条目。查询接口为 `/api/diagnosis/cases`。诊断记录不会自动修改检索或生产配置。

候选检索评估需要 Milvus、知识库和嵌入/重排上游可用。结构化报告包含总体/分桶指标、失败样本及相对基线逐题变化。显式传入评估集路径、用途和正在使用的知识库版本标识：

```bash
make eval-candidates DATASET=tests/data/eval_ch11_validation.jsonl DATASET_ROLE=validation KB_VERSION=kb-2026-10-20
```

先在完全相同的验证集、知识版本、模型和代码版本上重复至少三次基线，为每次运行指定独立报告路径。校准脚本用重复运行的中位数与最大观测偏差估计波动范围；不可回答题拒答下限、可回答题误拒上限和最终最少样本数由运营方明确提供，不从数据里猜：

```bash
make eval-candidates DATASET=tests/data/eval_ch11_validation.jsonl DATASET_ROLE=validation KB_VERSION=kb-2026-10-20 CANDIDATES=baseline OUT=data/ch11/reports/base-1.json
make eval-candidates DATASET=tests/data/eval_ch11_validation.jsonl DATASET_ROLE=validation KB_VERSION=kb-2026-10-20 CANDIDATES=baseline OUT=data/ch11/reports/base-2.json
make eval-candidates DATASET=tests/data/eval_ch11_validation.jsonl DATASET_ROLE=validation KB_VERSION=kb-2026-10-20 CANDIDATES=baseline OUT=data/ch11/reports/base-3.json
make calibrate-release-thresholds REPORTS="data/ch11/reports/base-1.json data/ch11/reports/base-2.json data/ch11/reports/base-3.json" MIN_SAMPLES=30 MIN_ABSENT_REFUSAL_RATE=0.90 MAX_FALSE_REFUSAL_RATE=0.05
```

再用独立 `test` 数据集运行基线和验证集选定的候选。结果记录数据集哈希、知识库版本、知识源哈希、模型和 Git 版本。校准数据与测试数据、知识版本、模型或代码版本不匹配时，门禁会返回 `INCONCLUSIVE`：

```bash
make eval-candidates DATASET=tests/data/eval_ch11_test.jsonl DATASET_ROLE=test KB_VERSION=kb-2026-10-20 CANDIDATES="baseline recall_20"
make release-gate CANDIDATE=recall_20
```

候选评估覆盖检索指标和置信度闸的拒答代理指标，不替代端到端生成/忠实度评估，也不执行发布。验证/测试切分由 `uv run --no-project python scripts/split_eval_ch11.py` 从现有 RAG 评估标注集生成，并记录在 split manifest 中；历史 ch04 报告曾使用完整源集合，因此这份派生测试集适合本轮候选比较，但严格的最终效果声明仍应补充新的独立人工标注样本。

## 端口

| 端口 | 是什么 |
| - | - |
| 8000 | 应用 |
| 8101 / 8102 | 业务 MCP Server，物流 / 售后 |
| 8110 | ch10 主题分类器推理服务（`make classifier-up` 之后） |
| 3000 | Langfuse（`make langfuse-up` 之后） |
| 19530 | Milvus |

应用那几个页面：`/` 聊天、`/kb` 知识库录入、`/review` 飞轮待审、`/observability` 观测与成本、
`/topics` 主题分布、`/acceptance` 分类器验收。
