# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
"""ch09 数据飞轮流水线:分批标准化/问题归并 + 选择性独立复核 → 待审队列。

批处理语义(用户拍板:定时批处理,不做近实时):
- 游标 = low_confidence_questions.matched_review_id IS NULL,处理完回写,天然幂等可重跑;
- 每批最多 10 条案例共用一次归并调用;仅拟归并组再进行单独复核调用;
- 归并拒绝或复核失败时不合并;复核失败的整批留待下轮重试;
- 通过归并的案例共享一个待审条目,原始问法仍保留在问题池中;
- 命中任何状态的候选都只累加 occurrence_count(驳回即终审,不复活);
- 解析失败/幻觉 id:该条跳过并告警,游标留在原地下轮重试。
"""
import logging
import json
import unicodedata
from itertools import combinations
from typing import Literal

from pydantic import BaseModel, Field
from langchain_core.prompts import ChatPromptTemplate

from app.core import llm
from app.core.prompts import FLYWHEEL_NORMALIZE_PROMPT
from app.core.persisted_tasks import persisted_task_id, run_persisted_task
from app.db import repository
from app.kb import dedup

logger = logging.getLogger(__name__)


class NormalizeResult(BaseModel):
    normalized_question: str = Field(description="FAQ 式标准问题")
    matched_question_id: int | None = Field(default=None, description="命中候选 id,无同类为 null")
    ai_suggested_answer: str = Field(default="", description="示例答案备查")


class BatchNormalizeItem(BaseModel):
    source_id: int = Field(description="输入案例 id，必须原样返回")
    normalized_question: str = Field(description="适合审核的规范问法")
    matched_question_id: int | None = Field(default=None, description="已有候选 id；无可靠匹配时为 null")
    ai_suggested_answer: str = Field(default="", description="仅供人工审核的答案草稿")


class PairRelationProposal(BaseModel):
    source_id_a: int
    source_id_b: int
    relation: Literal["merge", "no_merge", "uncertain"]
    rationale: str = Field(min_length=3, max_length=1000)


class BatchNormalizeResult(BaseModel):
    items: list[BatchNormalizeItem] = Field(default_factory=list)
    pair_relations: list[PairRelationProposal] = Field(default_factory=list)


class MergeAuditDecision(BaseModel):
    group_id: str
    same_intent_and_rule: bool = Field(
        description="原始问题是否具有相同意图、适用规则和应答依据")
    reason: str = Field(default="", description="简要说明依据；不能只引用关键词相似")


class MergeAuditResult(BaseModel):
    decisions: list[MergeAuditDecision] = Field(default_factory=list)


FLYWHEEL_BATCH_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """你负责把一批客服失败案例整理成待审核的问题簇。
对每个输入案例返回一项，source_id 必须原样保留。

归并规则：
- 只有用户意图、适用业务规则和应答依据相同，才使用相同的规范问法。
- 仅关键词相似不够；例如积分抵扣订单与积分提现现金必须分开。
- 有条件、对象或政策差异时要分开；无法确认是否同一规则时也分开。
- 与已有候选明确属于同一问题时填写其 id；不确定时填 null。
- 答案草稿只能依据输入案例，不补充未出现的业务事实；只供人工审核，不是知识库内容。
- 对输入案例中的每一个无序案例对都输出 pair_relations：关系只能是 merge、no_merge 或 uncertain；必须包含所有组合，A-B 与 B-A 不可重复。
- pair 关系依据用户意图、适用规则和答案依据判断；不确定时必须弃权。pair 结果应与规范问法分组一致。
- 不要省略案例，不要生成输入中没有的 source_id。"""),
    ("human", "已有待审核候选（id 与规范问法）：\n{candidates}\n\n"
             "本批案例（JSON）：\n{cases}"),
])


FLYWHEEL_MERGE_AUDIT_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """你是独立的归并复核员。逐组检查原始问法是否能安全归并。
只有用户意图、适用业务规则和应答依据都相同，才将 same_intent_and_rule 设为 true。
关键词相似、同属一个大类或答案看起来相似都不足以归并。
例如积分抵扣订单与积分提现现金必须分开；不同对象、条件或业务状态也应分开。
信息不足或存在歧义时设为 false。你只审核问题能否合并，不批准答案，不批准写入知识库。
每个输入 group_id 必须恰好返回一项，不得增加或遗漏。"""),
    ("human", "待复核的归并假设（JSON）：\n{groups}"),
])


def _chain():
    return FLYWHEEL_NORMALIZE_PROMPT | llm.structured(NormalizeResult)


async def normalize_and_match(raw_question: str, candidates: list[dict]) -> NormalizeResult:
    """一次模型调用输出 {normalized_question, matched_question_id, ai_suggested_answer}。"""
    cand_text = "\n".join(
        f"- id={c['id']}: {c['normalized_question']}" for c in candidates) or "(无候选)"
    return await _chain().ainvoke({"raw_question": raw_question, "candidates": cand_text})


async def normalize_batch(rows: list, candidates: list[dict]) -> BatchNormalizeResult:
    """批量规范化问法，并校验案例覆盖、候选 ID 和案例对关系。"""
    import json

    payload = [{"source_id": row.id, "question": row.raw_question} for row in rows]
    cand_text = "\n".join(
        f"- id={c['id']}: {c['normalized_question']}" for c in candidates
    ) or "(无候选)"
    chain = FLYWHEEL_BATCH_PROMPT | llm.structured(BatchNormalizeResult)
    result: BatchNormalizeResult = await chain.ainvoke({
        "candidates": cand_text,
        "cases": json.dumps(payload, ensure_ascii=False),
    })

    expected = {row.id for row in rows}
    returned = [item.source_id for item in result.items]
    if len(returned) != len(set(returned)) or set(returned) != expected:
        raise ValueError("batch normalization must return each input source_id exactly once")
    expected_pairs = {tuple(sorted(pair)) for pair in combinations(expected, 2)}
    actual_pairs = [tuple(sorted((pair.source_id_a, pair.source_id_b)))
                    for pair in result.pair_relations]
    if (len(actual_pairs) != len(set(actual_pairs)) or set(actual_pairs) != expected_pairs
            or any(a == b for a, b in actual_pairs)):
        raise ValueError("batch normalization must classify every unordered case pair exactly once")
    candidate_ids = {c["id"] for c in candidates}
    for item in result.items:
        if not item.normalized_question.strip():
            raise ValueError(f"empty normalized question for source_id={item.source_id}")
        if item.matched_question_id is not None and item.matched_question_id not in candidate_ids:
            raise ValueError(f"unknown matched candidate id for source_id={item.source_id}")
    candidate_by_question = {
        dedup.normalize_question(candidate["normalized_question"]): candidate["id"]
        for candidate in candidates
    }
    normalized_targets = {}
    for item in result.items:
        key = dedup.normalize_question(item.normalized_question)
        candidate_id = item.matched_question_id or candidate_by_question.get(key)
        normalized_targets[item.source_id] = (f"existing:{candidate_id}" if candidate_id is not None
                                              else f"batch:{key}")
    for pair in result.pair_relations:
        same_group = normalized_targets[pair.source_id_a] == normalized_targets[pair.source_id_b]
        if (pair.relation == "merge") != same_group and pair.relation != "uncertain":
            raise ValueError("pair relation conflicts with normalized-question grouping")
    return result


async def audit_proposed_merges(rows: list, result: BatchNormalizeResult,
                               candidates: list[dict]) -> tuple[dict[int, bool], bool]:
    """用独立模型调用复核已提出的合并组；没有合并候选时不额外调用模型。"""
    import json

    by_id = {item.source_id: item for item in result.items}
    row_by_id = {row.id: row for row in rows}
    candidate_by_id = {c["id"]: c["normalized_question"] for c in candidates}
    candidate_by_question = {
        dedup.normalize_question(c["normalized_question"]): c["id"]
        for c in candidates
    }
    targets: dict[str, list[int]] = {}
    target_candidate: dict[str, int | None] = {}
    for row in rows:
        item = by_id[row.id]
        candidate_id = item.matched_question_id
        canonical_key = dedup.normalize_question(item.normalized_question)
        if candidate_id is None:
            candidate_id = candidate_by_question.get(canonical_key)
        if candidate_id is not None:
            target = f"existing:{candidate_id}"
        else:
            # Include the canonical text in the target so unrelated or rejected
            # groups can never collide merely because their hashes/ids overlap.
            target = f"batch:{canonical_key}"
        targets.setdefault(target, []).append(row.id)
        target_candidate[target] = candidate_id

    hypotheses = []
    for target, source_ids in targets.items():
        candidate_id = target_candidate[target]
        if candidate_id is None and len(source_ids) < 2:
            continue
        hypotheses.append({
            "group_id": target,
            "candidate_question": candidate_by_id.get(candidate_id) if candidate_id else None,
            "proposed_question": by_id[source_ids[0]].normalized_question,
            "cases": [{"source_id": source_id,
                       "question": row_by_id[source_id].raw_question}
                      for source_id in source_ids],
        })
    if not hypotheses:
        return {}, False

    chain = FLYWHEEL_MERGE_AUDIT_PROMPT | llm.structured(MergeAuditResult)
    prompt_inputs = {"groups": json.dumps(hypotheses, ensure_ascii=False)}

    async def _invoke_and_validate() -> MergeAuditResult:
        value: MergeAuditResult = await chain.ainvoke(prompt_inputs)
        expected_ids = {group["group_id"] for group in hypotheses}
        returned_ids = [decision.group_id for decision in value.decisions]
        if len(returned_ids) != len(set(returned_ids)) or set(returned_ids) != expected_ids:
            raise ValueError("merge audit must return each proposed group exactly once")
        return value

    audited, invoked_now = await run_persisted_task(
        task_type="merge_audit",
        input_payload={"groups": hypotheses},
        prompt=FLYWHEEL_MERGE_AUDIT_PROMPT,
        schema=MergeAuditResult,
        call_type="merge_audit",
        invoke=_invoke_and_validate,
    )
    expected_ids = {group["group_id"] for group in hypotheses}
    returned_ids = [decision.group_id for decision in audited.decisions]
    if len(returned_ids) != len(set(returned_ids)) or set(returned_ids) != expected_ids:
        raise ValueError("merge audit must return each proposed group exactly once")
    decisions = {decision.group_id: decision.same_intent_and_rule
                 for decision in audited.decisions}
    return ({source_id: decisions[target]
             for target, source_ids in targets.items() if target in decisions
             for source_id in source_ids}, invoked_now)


async def process_pending(limit: int = 50, batch_size: int = 10) -> dict:
    """处理未归并问题：质量过滤 → 批量归并 → 按需复核 → 写入待审队列。"""
    if limit < 1 or batch_size < 1:
        raise ValueError("limit and batch_size must be at least 1")
    await repository.expire_flywheel_task_results()
    rows = await repository.fetch_unmatched_low_conf(limit)
    stats = {"processed": 0, "merged": 0, "created": 0, "skipped": 0,
             "ignored": 0, "model_calls": 0, "audit_calls": 0, "batches": 0,
             "recovered_tasks": 0}
    eligible = []
    ignored = []
    for row in rows:
        issue = _question_quality_issue(row.raw_question)
        if issue:
            ignored.append((row.id, issue))
        else:
            eligible.append(row)
    for case_id, issue in ignored:
        stats["ignored"] += await repository.mark_low_confidence_ignored(
            [case_id], issue)
    rows = eligible
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        # 每批取一次候选；上一批新建的问题会成为下一批的匹配候选。
        fetched = await repository.list_review_candidates(limit=201)
        truncated = len(fetched) > 200
        candidates = fetched[:200]
        try:
            # 主分析失败则整批保留待重试，避免只写入不完整的归并结果。
            normalize_payload = {
                "cases": [{"source_id": row.id, "question": row.raw_question}
                          for row in batch],
                "candidates": candidates,
            }
            merge_task_id = persisted_task_id(
                task_type="normalize_batch", input_payload=normalize_payload,
                prompt=FLYWHEEL_BATCH_PROMPT, schema=BatchNormalizeResult)
            result, invoked_now = await run_persisted_task(
                task_type="normalize_batch",
                input_payload=normalize_payload,
                prompt=FLYWHEEL_BATCH_PROMPT,
                schema=BatchNormalizeResult,
                call_type="normalize_batch",
                invoke=lambda: normalize_batch(batch, candidates),
            )
            stats["model_calls"] += int(invoked_now)
            stats["recovered_tasks"] += int(not invoked_now)
        except Exception:
            logger.warning("flywheel 批量归并失败 rows=%s(整批跳过,下轮重试)",
                           [row.id for row in batch], exc_info=True)
            stats["skipped"] += len(batch)
            continue
        stats["batches"] += 1

        by_id = {item.source_id: item for item in result.items}
        candidate_by_question = {
            dedup.normalize_question(c["normalized_question"]): c["id"]
            for c in candidates
        }
        targets: dict[int, tuple[str, int | None]] = {}
        for row in batch:
            item = by_id[row.id]
            canonical_key = dedup.normalize_question(item.normalized_question)
            mid = item.matched_question_id
            if mid is None:
                # Exact canonical equality is a deterministic match, even if the model
                # omitted an existing candidate id.
                mid = candidate_by_question.get(canonical_key)
            target = f"existing:{mid}" if mid is not None else f"batch:{canonical_key}"
            targets[row.id] = (target, mid)

        approved_by_source: dict[int, bool] = {}
        try:
            approved_by_source, audit_invoked = await audit_proposed_merges(
                batch, result, candidates)
            stats["audit_calls"] += int(audit_invoked)
            stats["recovered_tasks"] += int(not audit_invoked and bool(approved_by_source))
        except Exception:
            logger.warning("flywheel 归并复核失败 rows=%s(整批跳过,下轮重试)",
                           [row.id for row in batch], exc_info=True)
            stats["skipped"] += len(batch)
            continue

        assignments = []
        for row in batch:
            item = by_id[row.id]
            target, candidate_id = targets[row.id]
            # A rejected or uncertain merge is split into a standalone review item.
            # Its original wording remains available through the linked low-confidence row.
            if row.id in approved_by_source and not approved_by_source[row.id]:
                target = f"separate:{row.id}"
                candidate_id = None
            assignments.append({
                "lcq_id": row.id,
                "group_key": target,
                "merge_task_id": merge_task_id,
                "review_id": candidate_id,
                "normalized_question": item.normalized_question,
                "ai_suggested_answer": item.ai_suggested_answer or None,
            })

        pair_relations = []
        for pair in result.pair_relations:
            review_status = "pending"
            review_source = None
            if (pair.relation == "merge"
                    and targets[pair.source_id_a][0] == targets[pair.source_id_b][0]
                    and pair.source_id_a in approved_by_source):
                review_status = ("confirmed" if approved_by_source[pair.source_id_a]
                                 else "rejected")
                review_source = "model"
            pair_relations.append({
                "source_id_a": pair.source_id_a,
                "source_id_b": pair.source_id_b,
                "merge_task_id": merge_task_id,
                "relation_proposed": pair.relation,
                "review_status": review_status,
                "review_source": review_source,
                "rationale": pair.rationale,
            })

        # Queue writes and all source links commit as one transaction. If any
        # row fails, the entire batch rolls back and its persisted model outputs
        # can be reused on the next run without duplicated candidates/counts.
        write_stats = await repository.complete_flywheel_batch(
            assignments, pair_relations=pair_relations)
        for key in ("processed", "created", "merged", "skipped"):
            stats[key] += write_stats[key]
    return stats


def _question_quality_issue(question: str) -> str | None:
    """Reject obvious fragments/noise before they become review candidates.

    Keep the source row for audit, but require at least two letter-like
    characters and reject numeric-only input. This is a conservative intake
    gate, not an intent classifier.
    """
    normalized = unicodedata.normalize("NFKC", question or "").strip()
    letter_count = sum(unicodedata.category(char).startswith("L") for char in normalized)
    if letter_count == 0:
        return "numeric_or_symbol_only"
    if letter_count < 2:
        return "insufficient_text_for_review"
    return None
