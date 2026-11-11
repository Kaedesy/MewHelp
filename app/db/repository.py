# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
"""数据库访问层：封装问题收集、诊断/归并持久化、审核及知识发布记录。"""
from datetime import datetime, timedelta
import hashlib
import json
import uuid

from sqlalchemy import case, func, select, update

import app.db.base as db          # 用模块属性引用,便于测试 monkeypatch async_session
from app.config import settings
from app.db.models import (
    Conversation,
    ConversationSummary,
    DiagnosisCase,
    DiagnosisFault,
    DiagnosisReviewEvent,
    DiagnosisTask,
    ActiveRelease,
    CanonicalQuestion,
    EvolutionRelease,
    EvalRun,
    FaithCase,
    FlywheelGroupResult,
    FlywheelPairRelation,
    FlywheelPairReviewEvent,
    FlywheelInvocation,
    FlywheelTask,
    KnowledgeChunk,
    KnowledgeSnapshot,
    LowConfidenceQuestion,
    Message,
    QaExtractionStaging,
    ReviewQueue,
    ReleaseEvent,
    SystemConfigVersion,
    Ticket,
    ToolAuditLog,
    TopicClassification,
)

_TICKET_SEQ = 0


def _case_diagnosis_summary(tasks: list[DiagnosisTask], faults: list[DiagnosisFault]) -> str:
    if not tasks:
        return "in_progress"
    if any(task.execution_status == "failed" for task in tasks):
        return "blocked"
    confirmed = any(item.status == "confirmed" for item in faults)
    unresolved_faults = any(item.status == "proposed" for item in faults)
    uncertain_faults = any(item.status == "uncertain" for item in faults)
    incomplete = any(task.execution_status != "completed" for task in tasks)
    unresolved_outcome = any(task.outcome_final is None for task in tasks)
    inconclusive = any(task.outcome_final == "inconclusive" for task in tasks)
    if incomplete or unresolved_outcome or unresolved_faults:
        return "partially_diagnosed" if confirmed else "in_progress"
    if confirmed and (inconclusive or uncertain_faults):
        return "partially_diagnosed"
    if confirmed:
        return "faults_found"
    if inconclusive or uncertain_faults:
        return "inconclusive"
    return "no_fault_found"


async def get_flywheel_task(task_id: str) -> dict | None:
    async with db.async_session() as s:
        row = await s.get(FlywheelTask, task_id)
        if row is None:
            return None
        return {
            "task_id": row.task_id,
            "task_type": row.task_type,
            "execution_status": row.execution_status,
            "attempts": row.attempts,
            "result_json": row.result_json,
        }


async def start_flywheel_task(*, task_id: str, task_type: str,
                              input_fingerprint: str,
                              analysis_config_version: str,
                              output_schema_version: str) -> int:
    """Mark one logical task running and return its incremented attempt count."""
    async with db.async_session() as s:
        row = await s.scalar(select(FlywheelTask).where(
            FlywheelTask.task_id == task_id).with_for_update())
        if row is None:
            row = FlywheelTask(
                task_id=task_id, task_type=task_type,
                input_fingerprint=input_fingerprint,
                analysis_config_version=analysis_config_version,
                output_schema_version=output_schema_version,
                execution_status="running", attempts=1,
            )
            s.add(row)
        else:
            if row.execution_status == "completed" and row.result_json is not None:
                return row.attempts
            row.execution_status = "running"
            row.attempts += 1
            row.last_error_type = None
        await s.commit()
        return row.attempts


async def finish_flywheel_task(task_id: str, *, status: str,
                               result_json: dict | None = None,
                               error_type: str | None = None) -> None:
    if status not in {"completed", "retryable", "failed"}:
        raise ValueError(f"invalid flywheel task status: {status}")
    async with db.async_session() as s:
        row = await s.get(FlywheelTask, task_id)
        if row is None:
            raise ValueError(f"flywheel task {task_id} does not exist")
        # A completed result is immutable. A replay may observe it, but cannot
        # replace a result that has already passed validation and been stored.
        if row.execution_status == "completed":
            return
        row.execution_status = status
        if status == "completed":
            row.result_json = result_json
            row.result_expires_at = datetime.utcnow() + timedelta(days=30)
            row.last_error_type = None
        else:
            row.last_error_type = error_type
        await s.commit()


async def start_flywheel_invocation(*, invocation_id: str, task_id: str,
                                    call_type: str) -> None:
    async with db.async_session() as s:
        s.add(FlywheelInvocation(
            invocation_id=invocation_id, task_id=task_id,
            call_type=call_type, status="running",
        ))
        await s.commit()


async def finish_flywheel_execution(invocation_id: str, task_id: str, *,
                                    task_status: str,
                                    result_json: dict | None = None,
                                    response_hash: str | None = None,
                                    error_type: str | None = None) -> None:
    """Finalize one invocation and its logical task atomically."""
    if task_status not in {"completed", "retryable", "failed"}:
        raise ValueError(f"invalid flywheel task status: {task_status}")
    async with db.async_session() as s:
        task = await s.get(FlywheelTask, task_id)
        invocation = await s.get(FlywheelInvocation, invocation_id)
        if task is None or invocation is None:
            raise ValueError("flywheel task or invocation does not exist")
        invocation.status = "completed" if task_status == "completed" else "failed"
        invocation.response_hash = response_hash
        invocation.error_type = error_type
        invocation.finished_at = datetime.utcnow()
        if task.execution_status != "completed":
            task.execution_status = task_status
            if task_status == "completed":
                task.result_json = result_json
                task.result_expires_at = datetime.utcnow() + timedelta(days=30)
                task.last_error_type = None
            else:
                task.last_error_type = error_type
        await s.commit()


async def expire_flywheel_task_results() -> int:
    """Drop persisted semantic outputs after 30 days; retain non-content audit hashes."""
    async with db.async_session() as s:
        result = await s.execute(
            update(FlywheelTask)
            .where(FlywheelTask.result_expires_at <= datetime.utcnow())
            .values(result_json=None, result_expires_at=None,
                    execution_status="expired", attempts=0)
        )
        await s.commit()
        return result.rowcount or 0


def _gen_ticket_no() -> str:
    global _TICKET_SEQ
    _TICKET_SEQ += 1
    return f"T{datetime.now():%Y%m%d%H%M%S}{_TICKET_SEQ:03d}"


async def create_conversation(user_id: str) -> int:
    async with db.async_session() as s:
        conv = Conversation(user_id=user_id)
        s.add(conv)
        await s.commit()
        return conv.id


async def get_conversation(conversation_id: int) -> Conversation | None:
    async with db.async_session() as s:
        return await s.get(Conversation, conversation_id)


async def append_message(
    conversation_id: int,
    role: str,
    content: str | None = None,
    tool_calls: list | None = None,
    tool_call_id: str | None = None,
) -> int:
    async with db.async_session() as s:
        msg = Message(
            conversation_id=conversation_id,
            role=role,
            content=content,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
        )
        s.add(msg)
        await s.commit()
        return msg.id


async def list_messages(conversation_id: int) -> list[Message]:
    async with db.async_session() as s:
        result = await s.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id)
        )
        return list(result.scalars())


async def create_ticket(conversation_id: int, description: str, ticket_type: str) -> str:
    ticket_no = _gen_ticket_no()
    async with db.async_session() as s:
        s.add(
            Ticket(
                ticket_no=ticket_no,
                conversation_id=conversation_id,
                description=description,
                ticket_type=ticket_type,
            )
        )
        conv = await s.get(Conversation, conversation_id)
        if conv is not None:
            conv.status = "已转人工"
        await s.commit()
    return ticket_no


# ---- ch07 会话上下文管理(滑窗 + 异步摘要)----


async def count_messages_after(conversation_id: int, after_id: int | None) -> int:
    """距上次摘要新增了多少条(after_id 空 = 从未摘要,数全量)。摘要触发判据。
    注:此处数全角色,摘要源 list_dialog_messages 只取 user/assistant——当前一致
    (tool 不落库);若未来 tool 行落库,两处口径需同步收紧。"""
    async with db.async_session() as s:
        q = (select(func.count()).select_from(Message)
             .where(Message.conversation_id == conversation_id))
        if after_id:
            q = q.where(Message.id > after_id)
        return int((await s.execute(q)).scalar_one())


async def list_dialog_messages(conversation_id: int) -> list[Message]:
    """user/assistant 消息按 id 升序(摘要任务源数据;tool 行不落库,过滤一道保险)。"""
    async with db.async_session() as s:
        result = await s.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id,
                   Message.role.in_(("user", "assistant")))
            .order_by(Message.id)
        )
        return list(result.scalars())


async def list_conversations(user_id: str, limit: int = 50) -> list[dict]:
    """某用户的会话列表(新在前,带首问预览 + 有无摘要标记),前端多会话切换用。"""
    async with db.async_session() as s:
        convs = list((await s.execute(
            select(Conversation).where(Conversation.user_id == user_id)
            .order_by(Conversation.id.desc()).limit(limit)
        )).scalars())
        out = []
        for c in convs:
            first = (await s.execute(
                select(Message.content)
                .where(Message.conversation_id == c.id, Message.role == "user")
                .order_by(Message.id).limit(1)
            )).scalar()
            out.append({
                "id": c.id, "status": c.status,
                "preview": (first or "(空会话)")[:40],
                "has_summary": bool(c.summary),
                "updated_at": c.updated_at.isoformat() if c.updated_at else None,
            })
        return out


async def update_conversation_summary(conversation_id: int, summary: str, upto_msg_id: int) -> None:
    """摘要成功后原子更新两字段(一起写,不会出现摘要新边界旧)。"""
    async with db.async_session() as s:
        conv = await s.get(Conversation, conversation_id)
        if conv is not None:
            conv.summary = summary
            conv.summary_upto_msg_id = upto_msg_id
            await s.commit()


async def update_layer1_from(conversation_id: int, msg_id: int) -> None:
    """层 1 起点往后挪。只在超预算时挪一次,两次之间不动——边界稳,渲染才逐字节稳。"""
    async with db.async_session() as s:
        conv = await s.get(Conversation, conversation_id)
        if conv is not None:
            conv.layer1_from_msg_id = msg_id
            await s.commit()


async def append_summary_segment(conversation_id: int, from_msg_id: int,
                                 upto_msg_id: int, content: str) -> int:
    """追加一段摘要,同时把 conversations 的边界和拼好的投影一起更新。

    段落只追加不改写:已有的段不会再进模型,里面的订单号这类事实压过一次就定死了。
    conversations.summary 存的是最近若干段拼好的结果,读的时候少一次查询。"""
    async with db.async_session() as s:
        # 先把已有的几段读出来再 add:反过来的话 autoflush 会把新段也算进这次查询,
        # 拼投影时它就出现两次。
        prev = (await s.execute(
            select(ConversationSummary.seq, ConversationSummary.content)
            .where(ConversationSummary.conversation_id == conversation_id)
            .order_by(ConversationSummary.seq.desc())
            .limit(max(settings.summary_inject_segments - 1, 0))
        )).all()
        seq = (prev[0].seq if prev else 0) + 1
        s.add(ConversationSummary(conversation_id=conversation_id, seq=seq,
                                  from_msg_id=from_msg_id, upto_msg_id=upto_msg_id,
                                  content=content))
        conv = await s.get(Conversation, conversation_id)
        if conv is not None:
            conv.summary = "\n".join([*(r.content for r in reversed(prev)), content])
            conv.summary_upto_msg_id = upto_msg_id
        await s.commit()
        return seq


async def list_summary_segments(conversation_id: int, limit: int | None = None) -> list[str]:
    """取最近几段,按时间正序返回。再老的梗概对当前问题基本没用,不必全带。"""
    n = settings.summary_inject_segments if limit is None else limit
    async with db.async_session() as s:
        rows = (await s.execute(
            select(ConversationSummary.content)
            .where(ConversationSummary.conversation_id == conversation_id)
            .order_by(ConversationSummary.seq.desc())
            .limit(n)
        )).scalars().all()
        return list(reversed(rows))


# ---- ch03 知识库 chunk / 暂存表 ----


async def insert_knowledge_chunk(
    category: str, questions: str, answer: str,
    section_path: str | None = None, content_type: str | None = None,
    is_key_clause: int = 0,
) -> int:
    async with db.async_session() as s:
        row = KnowledgeChunk(
            category=category, questions=questions, answer=answer,
            section_path=section_path, content_type=content_type,
            is_key_clause=is_key_clause,
        )
        s.add(row)
        await s.commit()
        return row.id


async def list_pending_chunks() -> list[KnowledgeChunk]:
    async with db.async_session() as s:
        result = await s.execute(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.vectorize_status == "pending")
            .order_by(KnowledgeChunk.id)
        )
        return list(result.scalars())


async def list_vectorized_chunks() -> list[dict]:
    async with db.async_session() as s:
        rows = (await s.execute(select(KnowledgeChunk).where(
            KnowledgeChunk.vectorize_status == "done").order_by(KnowledgeChunk.id))).scalars()
        return [{"id": row.id, "category": row.category, "questions": row.questions,
                 "answer": row.answer, "section_path": row.section_path,
                 "content_type": row.content_type, "is_key_clause": row.is_key_clause}
                for row in rows]


async def save_knowledge_snapshot(*, snapshot_id: str, collection_name: str,
                                 manifest_sha256: str, manifest: dict,
                                 created_by: str, status: str) -> None:
    async with db.async_session() as s:
        row = await s.get(KnowledgeSnapshot, snapshot_id)
        if row is None:
            s.add(KnowledgeSnapshot(
                snapshot_id=snapshot_id, collection_name=collection_name,
                manifest_sha256=manifest_sha256, manifest=manifest,
                status=status, created_by=created_by))
        else:
            if row.manifest_sha256 != manifest_sha256 or row.collection_name != collection_name:
                raise ValueError("snapshot IDs are immutable and cannot change their manifest")
            row.status = status
        await s.commit()


async def get_knowledge_snapshot(snapshot_id: str) -> dict | None:
    async with db.async_session() as s:
        row = await s.get(KnowledgeSnapshot, snapshot_id)
        if row is None:
            return None
        return {"snapshot_id": row.snapshot_id, "collection_name": row.collection_name,
                "manifest_sha256": row.manifest_sha256, "manifest": row.manifest,
                "status": row.status, "created_by": row.created_by}


async def save_system_config_version(*, config_version: str, config_sha256: str,
                                     config: dict, created_by: str) -> None:
    async with db.async_session() as s:
        row = await s.get(SystemConfigVersion, config_version)
        if row is None:
            s.add(SystemConfigVersion(
                config_version=config_version, config_sha256=config_sha256,
                config=config, created_by=created_by))
        elif row.config_sha256 != config_sha256:
            raise ValueError("configuration versions are immutable")
        await s.commit()


async def create_evolution_release(*, release_id: str, snapshot_id: str,
                                   config_version: str, evaluation: dict,
                                   approved_by: str) -> None:
    async with db.async_session() as s:
        row = await s.get(EvolutionRelease, release_id)
        if row is not None:
            if (row.snapshot_id != snapshot_id or row.config_version != config_version
                    or row.evaluation != evaluation or row.approved_by != approved_by):
                raise ValueError("release IDs are immutable")
            return
        s.add(EvolutionRelease(
            release_id=release_id, snapshot_id=snapshot_id,
            config_version=config_version, evaluation=evaluation,
            approved_by=approved_by, status="prepared"))
        await s.commit()


async def get_evolution_release(release_id: str) -> dict | None:
    async with db.async_session() as s:
        row = await s.get(EvolutionRelease, release_id)
        if row is None:
            return None
        snapshot = await s.get(KnowledgeSnapshot, row.snapshot_id)
        config = await s.get(SystemConfigVersion, row.config_version)
        if snapshot is None or config is None:
            return None
        return {"release_id": row.release_id, "snapshot_id": row.snapshot_id,
                "collection_name": snapshot.collection_name,
                "snapshot_manifest": snapshot.manifest,
                "config_version": row.config_version, "config": config.config,
                "evaluation": row.evaluation, "approved_by": row.approved_by}


async def get_active_release_id() -> str | None:
    async with db.async_session() as s:
        row = await s.get(ActiveRelease, 1)
        return row.release_id if row else None


async def set_active_release(*, release_id: str, actor: str, reason: str,
                             event_type: str = "activate") -> str | None:
    async with db.async_session() as s:
        pointer = await s.get(ActiveRelease, 1, with_for_update=True)
        previous = pointer.release_id if pointer else None
        if pointer is None:
            pointer = ActiveRelease(singleton_id=1, release_id=release_id)
            s.add(pointer)
        else:
            pointer.release_id = release_id
        s.add(ReleaseEvent(from_release_id=previous, to_release_id=release_id,
                           event_type=event_type, actor=actor, reason=reason))
        await s.commit()
        return previous


async def mark_chunk_vectorized(chunk_id: int, vector_id: str) -> None:
    async with db.async_session() as s:
        row = await s.get(KnowledgeChunk, chunk_id)
        if row is not None:
            row.vector_id = vector_id
            row.vectorize_status = "done"
            await s.commit()


async def set_chunk_neighbors(chunk_id: int, prev_id: int | None, next_id: int | None) -> None:
    async with db.async_session() as s:
        row = await s.get(KnowledgeChunk, chunk_id)
        if row is not None:
            row.prev_chunk_id = prev_id
            row.next_chunk_id = next_id
            await s.commit()


async def list_chunks_by_content_types(content_types) -> list[KnowledgeChunk]:
    """按 content_type 取块(文档类建库材料)。飞轮写回的块 content_type 不在这几类里,
    所以按这个条件取到的就是「来自 data/kb/*.md 的那部分」,补库改文件时只动它们。"""
    async with db.async_session() as s:
        result = await s.execute(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.content_type.in_(list(content_types)))
            .order_by(KnowledgeChunk.id)
        )
        return list(result.scalars())


async def repend_chunk_text(chunk_id: int, questions: str, answer: str) -> None:
    """改写一块的正文并退回 pending,等 vectorize_pending 重嵌。

    为什么不是「删掉重插」:Milvus 那边 upsert 的主键就是这个 id,原地重嵌等于原地更新,
    引用角标、prev/next 邻居指针、飞轮写回的块都不受影响。整库重建(kb-reset)会连飞轮
    审核通过写回的那些块一起清掉,补库这种小改动犯不上。"""
    async with db.async_session() as s:
        row = await s.get(KnowledgeChunk, chunk_id)
        if row is not None:
            row.questions = questions
            row.answer = answer
            row.vectorize_status = "pending"
            await s.commit()


async def count_chunks_by_status(status: str) -> int:
    async with db.async_session() as s:
        result = await s.execute(
            select(func.count()).select_from(KnowledgeChunk)
            .where(KnowledgeChunk.vectorize_status == status)
        )
        return int(result.scalar_one())


async def list_all_questions() -> list[str]:
    async with db.async_session() as s:
        result = await s.execute(select(KnowledgeChunk.questions))
        return list(result.scalars())


async def count_chunks_by_content_types(content_types) -> int:
    async with db.async_session() as s:
        result = await s.execute(
            select(func.count()).select_from(KnowledgeChunk)
            .where(KnowledgeChunk.content_type.in_(list(content_types)))
        )
        return int(result.scalar_one())


async def knowledge_stats() -> dict:
    """知识库盘点:总块数 + 双写状态分布 + 内容类型分布 + 关键条款数。
    录入页的库存概览一次拿齐,不必让页面发四五个请求各问一个数。"""
    async with db.async_session() as s:
        total = (await s.execute(select(func.count()).select_from(KnowledgeChunk))).scalar_one()
        by_status = {k: int(v) for k, v in (await s.execute(
            select(KnowledgeChunk.vectorize_status, func.count())
            .group_by(KnowledgeChunk.vectorize_status))).all()}
        by_type = {(k or "未标注"): int(v) for k, v in (await s.execute(
            select(KnowledgeChunk.content_type, func.count())
            .group_by(KnowledgeChunk.content_type))).all()}
        key_clause = (await s.execute(
            select(func.count()).select_from(KnowledgeChunk)
            .where(KnowledgeChunk.is_key_clause == 1))).scalar_one()
    return {"total": int(total), "pending": by_status.get("pending", 0),
            "done": by_status.get("done", 0), "by_content_type": by_type,
            "key_clause": int(key_clause)}


async def list_recent_chunks(limit: int = 20) -> list[KnowledgeChunk]:
    async with db.async_session() as s:
        result = await s.execute(
            select(KnowledgeChunk).order_by(KnowledgeChunk.id.desc()).limit(limit)
        )
        return list(result.scalars())


async def list_chunk_pairs() -> list[tuple[str, str]]:
    """全表的 (questions, answer) 对,给录入查重算指纹。
    只按问法查重会误杀同一节切出来的多块——表格按行拆、超长散文递归切,这些块共用节标题,
    问法一模一样、正文各不相同,是该留的。"""
    async with db.async_session() as s:
        result = await s.execute(select(KnowledgeChunk.questions, KnowledgeChunk.answer))
        return [(q, a) for q, a in result.all()]


async def staging_stats() -> dict:
    """抽 QA 暂存表盘点:三个状态各几条 + 批次数 + 最近批次号。
    「先进暂存、整体去重再入库」这一步在页面上要看得见,靠的就是这三个状态的落差。"""
    async with db.async_session() as s:
        counts = {k: int(v) for k, v in (await s.execute(
            select(QaExtractionStaging.status, func.count())
            .group_by(QaExtractionStaging.status))).all()}
        batches = (await s.execute(
            select(func.count(func.distinct(QaExtractionStaging.batch_no))))).scalar_one()
        latest = (await s.execute(
            select(QaExtractionStaging.batch_no)
            .order_by(QaExtractionStaging.id.desc()).limit(1))).scalar_one_or_none()
    return {"counts": {k: counts.get(k, 0)
                       for k in ("extracted", "kept", "discarded")},
            "total": sum(counts.values()), "batches": int(batches), "latest_batch": latest}


async def insert_staging(batch_no: str, source_ref: str | None, question: str, answer: str) -> int:
    async with db.async_session() as s:
        row = QaExtractionStaging(
            batch_no=batch_no, source_ref=source_ref, question=question, answer=answer
        )
        s.add(row)
        await s.commit()
        return row.id


async def list_staging_by_status(status: str) -> list[QaExtractionStaging]:
    async with db.async_session() as s:
        result = await s.execute(
            select(QaExtractionStaging)
            .where(QaExtractionStaging.status == status)
            .order_by(QaExtractionStaging.id)
        )
        return list(result.scalars())


async def list_staging_by_ids(ids: list[int], status: str | None = None) -> list[QaExtractionStaging]:
    """按 id 取暂存行,可再限定状态。人工采纳/弃用时用它——限定 status='kept' 就能挡住
    重复采纳(点两次只有第一次拿得到行)和拿已弃用的行来入库。"""
    if not ids:
        return []
    async with db.async_session() as s:
        q = select(QaExtractionStaging).where(QaExtractionStaging.id.in_(ids))
        if status is not None:
            q = q.where(QaExtractionStaging.status == status)
        return list((await s.execute(q.order_by(QaExtractionStaging.id))).scalars())


async def set_staging_status(ids: list[int], status: str) -> None:
    if not ids:
        return
    async with db.async_session() as s:
        for i in ids:
            row = await s.get(QaExtractionStaging, i)
            if row is not None:
                row.status = status
        await s.commit()


async def insert_low_confidence(
    conversation_id: int | None, raw_question: str, source: str, reason: str | None,
    retrieved_chunks: list | None = None,
) -> int:
    """将拒答或用户反馈案例写入待处理问题池，并保留当时召回快照。"""
    """将拒答或用户反馈案例写入待处理问题池，并保留当时召回快照。"""
    async with db.async_session() as s:
        row = LowConfidenceQuestion(
            conversation_id=conversation_id, raw_question=raw_question,
            source=source, reason=reason, retrieved_chunks=retrieved_chunks,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row.id


async def upsert_diagnosis_case(case: dict) -> DiagnosisCase:
    """创建或更新一个需跟进的案例；case_id 是应用生成的幂等键。"""
    diagnosis_tasks = case.get("_diagnosis_tasks") or []
    case_fields = {key: value for key, value in case.items() if not key.startswith("_")}
    async with db.async_session() as s:
        row = await s.scalar(select(DiagnosisCase).where(
            DiagnosisCase.case_id == case_fields["case_id"]))
        if row is None:
            row = DiagnosisCase(**case_fields)
            s.add(row)
        else:
            human_confirmed = row.diagnosis_status in {"confirmed", "false_positive"}
            for key, value in case_fields.items():
                if key != "case_id" and value is not None:
                    if human_confirmed and key in {
                        "failure_type", "predicted_failure_type", "human_failure_type",
                        "diagnosis_status", "diagnosis_reason",
                    }:
                        continue
                    setattr(row, key, value)

        for proposed_task in diagnosis_tasks:
            task = await s.get(DiagnosisTask, proposed_task["task_id"])
            if task is None:
                task = DiagnosisTask(
                    task_id=proposed_task["task_id"], case_id=case_fields["case_id"],
                    diagnostic_scope=proposed_task["diagnostic_scope"],
                    execution_status=proposed_task["execution_status"],
                    outcome_proposed=proposed_task.get("outcome_proposed"),
                    outcome_final=proposed_task.get("outcome_final"),
                    evidence_refs=proposed_task.get("evidence_refs"),
                    error_type=proposed_task.get("error_type"),
                    attempts=proposed_task.get("attempts", 0),
                )
                s.add(task)
                await s.flush()
            elif task.outcome_final is None:
                task.execution_status = proposed_task["execution_status"]
                task.outcome_proposed = proposed_task.get("outcome_proposed")
                task.evidence_refs = proposed_task.get("evidence_refs")

            for proposed_fault in proposed_task.get("faults", []):
                fault = await s.get(DiagnosisFault, proposed_fault["fault_id"])
                if fault is None:
                    s.add(DiagnosisFault(
                        fault_id=proposed_fault["fault_id"], task_id=task.task_id,
                        fault_type=proposed_fault["fault_type"],
                        status=proposed_fault.get("status", "proposed"),
                        proposal_source=proposed_fault.get("proposal_source", "rule"),
                        review_source=proposed_fault.get("review_source"),
                        confirmation_source=proposed_fault.get("confirmation_source"),
                        evidence_refs=proposed_fault.get("evidence_refs"),
                        reason=proposed_fault.get("reason", ""),
                    ))
                elif fault.status == "proposed":
                    fault.evidence_refs = proposed_fault.get("evidence_refs")
                    fault.reason = proposed_fault.get("reason", fault.reason)

        await s.flush()
        tasks = list((await s.execute(select(DiagnosisTask).where(
            DiagnosisTask.case_id == case_fields["case_id"]))).scalars())
        task_ids = [task.task_id for task in tasks]
        faults = (list((await s.execute(select(DiagnosisFault).where(
            DiagnosisFault.task_id.in_(task_ids)))).scalars()) if task_ids else [])
        row.case_diagnosis_status = _case_diagnosis_summary(tasks, faults)
        await s.commit()
        await s.refresh(row)
        return row


async def list_diagnosis_cases(status: str | None = None, limit: int = 50,
                               failure_type: str | None = None,
                               offset: int = 0) -> list[DiagnosisCase]:
    async with db.async_session() as s:
        stmt = select(DiagnosisCase).order_by(DiagnosisCase.created_at.desc(), DiagnosisCase.id.desc())
        if status:
            stmt = stmt.where(DiagnosisCase.diagnosis_status == status)
        if failure_type:
            stmt = stmt.where(DiagnosisCase.failure_type == failure_type)
        return list((await s.execute(stmt.offset(offset).limit(limit))).scalars())


async def fetch_diagnosis_analysis_cases(scope: str, limit: int = 50) -> list[dict]:
    """取出尚未人工定案、且当前分析版本还未完成的诊断案例。"""
    async with db.async_session() as s:
        cases = list((await s.execute(
            select(DiagnosisCase)
            .where(DiagnosisCase.diagnosis_status.in_(("pending", "insufficient_data")))
            .order_by(DiagnosisCase.created_at, DiagnosisCase.id)
            .limit(max(limit * 3, limit))
        )).scalars())
        result = []
        for case in cases:
            task = await s.scalar(select(DiagnosisTask).where(
                DiagnosisTask.case_id == case.case_id,
                DiagnosisTask.diagnostic_scope == scope))
            if task and task.execution_status == "completed":
                continue
            if task and task.execution_status == "failed":
                continue
            result.append({
                "case_id": case.case_id, "question": case.question,
                "diagnosis_reason": case.diagnosis_reason,
                "retrieved_docs": case.retrieved_docs,
                "evidence_confidence": case.evidence_confidence,
                "selfcheck_result": case.selfcheck_result,
                "tool_status": case.tool_status,
                "final_answer": case.final_answer,
                "user_feedback": case.user_feedback,
                "topic_labels": case.topic_labels,
                "triage_attempts": task.attempts if task else 0,
            })
            if len(result) >= limit:
                break
        return result


async def persist_diagnosis_analysis(*, case_id: str, diagnostic_scope: str,
                                     task_id: str, topic_labels: list[str],
                                     proposal: dict | None = None,
                                     error_type: str | None = None) -> str:
    """保存单案例的有效初诊建议或失败状态；已完成人工处理的结论不被覆盖。"""
    async with db.async_session() as s:
        case = await s.scalar(select(DiagnosisCase).where(
            DiagnosisCase.case_id == case_id).with_for_update())
        if case is None:
            raise ValueError(f"diagnosis case {case_id} does not exist")
        case.topic_labels = topic_labels
        task = await s.get(DiagnosisTask, task_id)
        if task is None:
            task = DiagnosisTask(
                task_id=task_id, case_id=case_id,
                diagnostic_scope=diagnostic_scope,
                execution_status="pending", attempts=0,
            )
            s.add(task)
            await s.flush()
        if task.execution_status == "completed":
            return task.execution_status

        task.attempts += 1
        if proposal is None:
            task.execution_status = "failed" if task.attempts >= 3 else "retryable"
            task.error_type = error_type or "invalid_model_output"
            task.outcome_proposed = None
            task.outcome_final = None
        else:
            task.execution_status = "completed"
            task.error_type = None
            task.outcome_proposed = proposal["outcome_proposed"]
            task.outcome_final = None
            task.evidence_refs = proposal.get("evidence_refs") or None
            for item in proposal.get("faults", []):
                evidence_refs = item["evidence_refs"]
                fault_id = hashlib.sha256((
                    f"{task_id}:{item['fault_type']}:" + json.dumps(
                        evidence_refs, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"))
                ).encode("utf-8")).hexdigest()
                fault = await s.get(DiagnosisFault, fault_id)
                if fault is None:
                    s.add(DiagnosisFault(
                        fault_id=fault_id, task_id=task_id,
                        fault_type=item["fault_type"], status="proposed",
                        proposal_source="model", review_source=None,
                        confirmation_source=None, evidence_refs=evidence_refs,
                        reason=item.get("reason", ""),
                    ))

        await s.flush()
        tasks = list((await s.execute(select(DiagnosisTask).where(
            DiagnosisTask.case_id == case_id))).scalars())
        task_ids = [row.task_id for row in tasks]
        faults = (list((await s.execute(select(DiagnosisFault).where(
            DiagnosisFault.task_id.in_(task_ids)))).scalars()) if task_ids else [])
        case.case_diagnosis_status = _case_diagnosis_summary(tasks, faults)
        await s.commit()
        return task.execution_status


async def get_diagnosis_case(case_id: str) -> DiagnosisCase | None:
    async with db.async_session() as s:
        return await s.scalar(select(DiagnosisCase).where(DiagnosisCase.case_id == case_id))


async def list_diagnosis_tasks(case_id: str) -> list[dict]:
    async with db.async_session() as s:
        tasks = list((await s.execute(select(DiagnosisTask).where(
            DiagnosisTask.case_id == case_id).order_by(DiagnosisTask.created_at,
                                                        DiagnosisTask.task_id))).scalars())
        task_ids = [task.task_id for task in tasks]
        faults = (list((await s.execute(select(DiagnosisFault).where(
            DiagnosisFault.task_id.in_(task_ids)).order_by(DiagnosisFault.created_at,
                                                            DiagnosisFault.fault_id))).scalars())
                  if task_ids else [])
    faults_by_task: dict[str, list[dict]] = {}
    for fault in faults:
        faults_by_task.setdefault(fault.task_id, []).append({
            "fault_id": fault.fault_id, "fault_type": fault.fault_type,
            "status": fault.status, "proposal_source": fault.proposal_source,
            "review_source": fault.review_source,
            "confirmation_source": fault.confirmation_source,
            "evidence_refs": fault.evidence_refs, "reason": fault.reason,
        })
    return [{
        "task_id": task.task_id, "diagnostic_scope": task.diagnostic_scope,
        "execution_status": task.execution_status,
        "outcome_proposed": task.outcome_proposed,
        "outcome_final": task.outcome_final,
        "evidence_refs": task.evidence_refs, "error_type": task.error_type,
        "attempts": task.attempts, "faults": faults_by_task.get(task.task_id, []),
    } for task in tasks]


async def update_diagnosis_case(case_id: str, *, failure_type: str,
                                diagnosis_status: str, diagnosis_reason: str,
                                reviewer_id: str | None = None) -> DiagnosisCase | None:
    async with db.async_session() as s:
        row = await s.scalar(select(DiagnosisCase).where(DiagnosisCase.case_id == case_id))
        if row is None:
            return None
        row.failure_type = failure_type
        row.human_failure_type = failure_type
        row.diagnosis_status = diagnosis_status
        row.diagnosis_reason = diagnosis_reason

        tasks = list((await s.execute(select(DiagnosisTask).where(
            DiagnosisTask.case_id == case_id).with_for_update())).scalars())
        task_ids = [task.task_id for task in tasks]
        faults = (list((await s.execute(select(DiagnosisFault).where(
            DiagnosisFault.task_id.in_(task_ids)).with_for_update())).scalars())
                  if task_ids else [])
        matched_faults = [fault for fault in faults if fault.fault_type == failure_type]
        if diagnosis_status == "confirmed" and not matched_faults:
            # Human diagnosis can add a fault category that the automatic path
            # did not propose; preserve it as a separately sourced finding.
            task_id = hashlib.sha256(
                f"{case_id}:human_diagnosis:{failure_type}".encode("utf-8")).hexdigest()
            task = await s.get(DiagnosisTask, task_id)
            if task is None:
                task = DiagnosisTask(
                    task_id=task_id, case_id=case_id,
                    diagnostic_scope="human_diagnosis", execution_status="completed",
                    outcome_proposed="faults_found", outcome_final="faults_found",
                    evidence_refs=[{"source": "human_review", "case_id": case_id}],
                )
                s.add(task)
                await s.flush()
            evidence = [{"source": "human_review", "case_id": case_id}]
            fault_id = hashlib.sha256(
                f"{task_id}:{failure_type}".encode("utf-8")).hexdigest()
            fault = await s.get(DiagnosisFault, fault_id)
            if fault is None:
                fault = DiagnosisFault(
                    fault_id=fault_id, task_id=task_id, fault_type=failure_type,
                    status="confirmed", proposal_source="human", review_source="human",
                    confirmation_source="human", evidence_refs=evidence,
                    reason=diagnosis_reason,
                )
                s.add(fault)
                await s.flush()
            matched_faults = [fault]

        for fault in matched_faults:
            if diagnosis_status == "confirmed":
                fault.status = "confirmed"
                fault.confirmation_source = "human"
            elif diagnosis_status == "false_positive":
                fault.status = "rejected"
                fault.confirmation_source = None
            else:
                fault.status = "uncertain"
                fault.confirmation_source = None
            fault.review_source = "human"
            s.add(DiagnosisReviewEvent(
                fault_id=fault.fault_id, decision=diagnosis_status,
                reviewer_id=reviewer_id, comment=diagnosis_reason,
            ))

        # A case-level review of the primary category must not overwrite other
        # scopes. Recompute each affected task from its independently reviewed
        # fault items, keeping unresolved proposals unresolved.
        for task in tasks:
            task_faults = [fault for fault in faults if fault.task_id == task.task_id]
            if task.execution_status != "completed":
                continue
            if any(fault.status == "proposed" for fault in task_faults):
                task.outcome_final = None
            elif any(fault.status == "confirmed" for fault in task_faults):
                task.outcome_final = "faults_found"
            elif any(fault.status == "uncertain" for fault in task_faults):
                task.outcome_final = "inconclusive"
            elif task.outcome_proposed == "inconclusive":
                task.outcome_final = "inconclusive"
            else:
                task.outcome_final = "no_fault_found"

        tasks = list((await s.execute(select(DiagnosisTask).where(
            DiagnosisTask.case_id == case_id))).scalars())
        task_ids = [task.task_id for task in tasks]
        faults = (list((await s.execute(select(DiagnosisFault).where(
            DiagnosisFault.task_id.in_(task_ids)))).scalars()) if task_ids else [])
        row.case_diagnosis_status = _case_diagnosis_summary(tasks, faults)
        if failure_type == "knowledge_gap" and diagnosis_status == "confirmed":
            # Human-confirmed gaps enter the existing normalization/deduplication pipeline.
            # The unique diagnosis_case_id makes retries (or repeated PUT-like updates) idempotent.
            queued = await s.scalar(select(LowConfidenceQuestion).where(
                LowConfidenceQuestion.diagnosis_case_id == case_id))
            if queued is None:
                s.add(LowConfidenceQuestion(
                    conversation_id=row.conversation_id,
                    raw_question=row.question,
                    source="diagnosis_confirmed",
                    reason=f"人工确认知识缺口：{diagnosis_reason}",
                    retrieved_chunks=row.retrieved_docs,
                    diagnosis_case_id=case_id,
                ))
        await s.commit()
        await s.refresh(row)
        return row


async def diagnosis_label_summary() -> dict:
    """Agreement between retained automatic predictions and human-confirmed labels."""
    async with db.async_session() as s:
        rows = (await s.execute(select(
            DiagnosisCase.predicted_failure_type,
            DiagnosisCase.human_failure_type,
            DiagnosisCase.diagnosis_status,
        ).where(DiagnosisCase.human_failure_type.is_not(None)))).all()
        fault_rows = (await s.execute(select(
            DiagnosisFault.fault_type, DiagnosisFault.status,
            DiagnosisFault.proposal_source, DiagnosisFault.review_source,
        ))).all()
    labeled = [r for r in rows if r.diagnosis_status == "confirmed"]
    matched = sum(r.predicted_failure_type == r.human_failure_type for r in labeled)
    per_type: dict[str, dict[str, int]] = {}
    reviewed_faults = [r for r in fault_rows if r.review_source == "human"]
    for fault_type, status, proposal_source, _ in reviewed_faults:
        metric = per_type.setdefault(fault_type, {"tp": 0, "fp": 0, "fn": 0, "uncertain": 0})
        if status == "confirmed":
            metric["fn" if proposal_source == "human" else "tp"] += 1
        elif status == "rejected":
            metric["fp"] += 1
        elif status == "uncertain":
            metric["uncertain"] += 1
    fault_metrics = []
    for fault_type, counts in sorted(per_type.items()):
        precision_den = counts["tp"] + counts["fp"]
        recall_den = counts["tp"] + counts["fn"]
        precision = counts["tp"] / precision_den if precision_den else None
        recall = counts["tp"] / recall_den if recall_den else None
        f1 = (2 * precision * recall / (precision + recall)
              if precision is not None and recall is not None and precision + recall else 0.0)
        fault_metrics.append({
            "fault_type": fault_type, **counts,
            "precision": round(precision, 4) if precision is not None else None,
            "recall": round(recall, 4) if recall is not None else None,
            "f1": round(f1, 4), "support": recall_den,
        })
    return {
        "labeled_confirmed": len(labeled),
        "type_matches": matched,
        "type_agreement": round(matched / len(labeled), 4) if labeled else None,
        "false_positives": sum(r.diagnosis_status == "false_positive" for r in rows),
        "insufficient_data": sum(r.diagnosis_status == "insufficient_data" for r in rows),
        "fault_item_metrics": fault_metrics,
        "human_reviewed_fault_items": len(reviewed_faults),
        "human_reviewed_uncertain_fault_items": sum(r.status == "uncertain" for r in reviewed_faults),
    }


async def diagnosis_review_workload() -> dict:
    """Measure diagnosis volume and existing review-queue deduplication by failure type."""
    async with db.async_session() as s:
        rows = (await s.execute(select(
            DiagnosisCase.failure_type,
            DiagnosisCase.diagnosis_status,
            DiagnosisCase.case_diagnosis_status,
            DiagnosisCase.human_failure_type,
            DiagnosisCase.review_queue_id,
            ReviewQueue.review_status,
            ReviewQueue.occurrence_count,
        ).outerjoin(ReviewQueue, DiagnosisCase.review_queue_id == ReviewQueue.id))).all()

    groups: dict[str, dict] = {}
    for failure_type, status, case_status, human_type, queue_id, queue_status, occurrences in rows:
        group = groups.setdefault(failure_type, {
            "failure_type": failure_type, "raw_cases": 0, "pending_diagnosis": 0,
            "human_reviewed": 0, "partially_diagnosed": 0, "blocked": 0,
            "linked_cases": 0, "clusters": {},
        })
        group["raw_cases"] += 1
        group["pending_diagnosis"] += status == "pending"
        group["partially_diagnosed"] += case_status == "partially_diagnosed"
        group["blocked"] += case_status == "blocked"
        group["human_reviewed"] += human_type is not None or status in {
            "confirmed", "false_positive", "insufficient_data"}
        if queue_id is not None:
            group["linked_cases"] += 1
            group["clusters"][queue_id] = {
                "status": queue_status, "occurrences": occurrences or 0,
            }

    categories = []
    for group in groups.values():
        clusters = group.pop("clusters")
        cluster_count = len(clusters)
        linked_cases = group["linked_cases"]
        raw_cases = group["raw_cases"]
        categories.append({
            **group,
            "review_clusters": cluster_count,
            "open_review_clusters": sum(c["status"] == "待审" for c in clusters.values()),
            "cluster_occurrences": sum(c["occurrences"] for c in clusters.values()),
            "deduplication_rate": (round(1 - cluster_count / linked_cases, 4)
                                   if linked_cases else None),
            "human_review_coverage": (round(group["human_reviewed"] / raw_cases, 4)
                                      if raw_cases else None),
        })
    categories.sort(key=lambda item: (-item["raw_cases"], item["failure_type"]))
    return {"categories": categories,
            "totals": {"raw_cases": sum(x["raw_cases"] for x in categories),
                       "pending_diagnosis": sum(x["pending_diagnosis"] for x in categories),
                       "human_reviewed": sum(x["human_reviewed"] for x in categories),
                       "partially_diagnosed": sum(x["partially_diagnosed"] for x in categories),
                       "blocked": sum(x["blocked"] for x in categories),
                       "review_clusters": sum(x["review_clusters"] for x in categories),
                       "open_review_clusters": sum(x["open_review_clusters"] for x in categories)}}


async def list_conversations_with_messages() -> list[tuple[int, list[Message]]]:
    """挖知识用:每个会话及其按序消息(供拼成对话文本喂 LLM)。"""
    async with db.async_session() as s:
        conv_ids = list(
            (await s.execute(select(Conversation.id).order_by(Conversation.id))).scalars()
        )
        out: list[tuple[int, list[Message]]] = []
        for cid in conv_ids:
            msgs = list(
                (await s.execute(
                    select(Message).where(Message.conversation_id == cid).order_by(Message.id)
                )).scalars()
            )
            out.append((cid, msgs))
        return out


# ---- ch09 数据飞轮(待审队列 + 评估轮次)----


async def fetch_unmatched_low_conf(limit: int) -> list[LowConfidenceQuestion]:
    """飞轮处理游标:尚未归并(matched_review_id IS NULL)的池内问题,按 id 升序。"""
    async with db.async_session() as s:
        result = await s.execute(
            select(LowConfidenceQuestion)
            .where(LowConfidenceQuestion.matched_review_id.is_(None))
            .where(LowConfidenceQuestion.processing_status == "pending")
            .order_by(LowConfidenceQuestion.id).limit(limit)
        )
        return list(result.scalars())


async def mark_low_confidence_ignored(case_ids: list[int], note: str) -> int:
    if not case_ids:
        return 0
    async with db.async_session() as s:
        result = await s.execute(
            update(LowConfidenceQuestion)
            .where(LowConfidenceQuestion.id.in_(case_ids))
            .where(LowConfidenceQuestion.matched_review_id.is_(None))
            .where(LowConfidenceQuestion.processing_status == "pending")
            .values(processing_status="ignored", processing_note=note[:255])
        )
        await s.commit()
        return result.rowcount or 0


async def list_review_candidates(limit: int = 200) -> list[dict]:
    """查重候选:全部状态的缺口行(用户拍板:比全部,驳回即终审),updated_at 倒序截断防 token 爆。"""
    async with db.async_session() as s:
        result = await s.execute(
            select(ReviewQueue.id, ReviewQueue.normalized_question)
            .order_by(ReviewQueue.updated_at.desc()).limit(limit)
        )
        return [{"id": r.id, "normalized_question": r.normalized_question} for r in result]


async def insert_review_item(normalized_question: str, ai_suggested_answer: str | None) -> int:
    async with db.async_session() as s:
        canonical_question_id = str(uuid.uuid4())
        s.add(CanonicalQuestion(canonical_question_id=canonical_question_id,
                                normalized_question=normalized_question))
        row = ReviewQueue(normalized_question=normalized_question,
                          canonical_question_id=canonical_question_id,
                          ai_suggested_answer=ai_suggested_answer)
        s.add(row)
        await s.commit()
        return row.id


async def increment_occurrence(review_id: int) -> None:
    """查重命中:只累加次数,状态不动(命中已驳回/已通过也一样——驳回即终审)。
    原子 UPDATE,不走读改写,两批飞轮重叠时不丢计数。"""
    async with db.async_session() as s:
        await s.execute(
            update(ReviewQueue)
            .where(ReviewQueue.id == review_id)
            .values(occurrence_count=ReviewQueue.occurrence_count + 1)
        )
        await s.commit()


async def set_matched_review(lcq_id: int, review_id: int) -> None:
    async with db.async_session() as s:
        row = await s.get(LowConfidenceQuestion, lcq_id)
        if row is not None:
            row.matched_review_id = review_id
            row.processing_status = "queued"
            row.processing_note = None
            if row.diagnosis_case_id:
                case = await s.scalar(select(DiagnosisCase).where(
                    DiagnosisCase.case_id == row.diagnosis_case_id))
                if case is not None:
                    case.review_queue_id = review_id
            await s.commit()


async def complete_flywheel_batch(assignments: list[dict],
                                  pair_relations: list[dict] | None = None) -> dict[str, int]:
    """事务性写入归并分组、案例关联和审核关系；失败时整批回滚以支持安全重试。"""
    if not assignments:
        return {"processed": 0, "created": 0, "merged": 0, "skipped": 0}
    ordered = sorted(assignments, key=lambda item: item["lcq_id"])
    async with db.async_session() as s:
        rows: dict[int, LowConfidenceQuestion] = {}
        for item in ordered:
            row = await s.scalar(select(LowConfidenceQuestion).where(
                LowConfidenceQuestion.id == item["lcq_id"]).with_for_update())
            if row is not None:
                rows[item["lcq_id"]] = row

        group_reviews: dict[str, int] = {}
        group_members: dict[str, list[str]] = {}
        group_metadata: dict[str, dict] = {}
        stats = {"processed": 0, "created": 0, "merged": 0, "skipped": 0}
        for item in assignments:
            row = rows.get(item["lcq_id"])
            if row is None:
                raise ValueError(f"low-confidence case {item['lcq_id']} does not exist")
            if row.matched_review_id is not None:
                group_reviews.setdefault(item["group_key"], row.matched_review_id)
                stats["skipped"] += 1
                continue

            review_id = item.get("review_id") or group_reviews.get(item["group_key"])
            created = False
            if review_id is None:
                if not item.get("normalized_question"):
                    raise ValueError("normalized_question is required for a new review item")
                canonical_question_id = str(uuid.uuid4())
                s.add(CanonicalQuestion(
                    canonical_question_id=canonical_question_id,
                    normalized_question=item["normalized_question"],
                ))
                review = ReviewQueue(
                    canonical_question_id=canonical_question_id,
                    normalized_question=item["normalized_question"],
                    ai_suggested_answer=item.get("ai_suggested_answer"),
                )
                s.add(review)
                await s.flush()
                review_id = review.id
                created = True
            else:
                review = await s.scalar(select(ReviewQueue).where(
                    ReviewQueue.id == review_id).with_for_update())
                if review is None:
                    raise ValueError(f"review item {review_id} does not exist")
                review.occurrence_count += 1

            row.matched_review_id = review_id
            group_members.setdefault(item["group_key"], []).append(
                row.diagnosis_case_id or f"lcq:{row.id}")
            group_metadata.setdefault(item["group_key"], {
                "merge_task_id": item["merge_task_id"],
                "canonical_question_id": review.canonical_question_id,
                "normalized_question": review.normalized_question,
            })
            if row.diagnosis_case_id:
                case = await s.scalar(select(DiagnosisCase).where(
                    DiagnosisCase.case_id == row.diagnosis_case_id))
                if case is not None:
                    case.review_queue_id = review_id
            group_reviews.setdefault(item["group_key"], review_id)
            stats["processed"] += 1
            stats["created" if created else "merged"] += 1

        for group_key, members in group_members.items():
            metadata = group_metadata[group_key]
            member_ids = sorted(set(members))
            identity = json.dumps(member_ids, ensure_ascii=False, separators=(",", ":"))
            group_result_id = hashlib.sha256(
                f"{metadata['merge_task_id']}:{identity}".encode("utf-8")).hexdigest()
            content_sha = hashlib.sha256(json.dumps({
                "canonical_question_id": metadata["canonical_question_id"],
                "member_case_ids": member_ids,
                "normalized_question": metadata["normalized_question"],
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            saved = await s.get(FlywheelGroupResult, group_result_id)
            if saved is None:
                s.add(FlywheelGroupResult(
                    group_result_id=group_result_id,
                    merge_task_id=metadata["merge_task_id"],
                    canonical_question_id=metadata["canonical_question_id"],
                    member_case_ids=member_ids,
                    normalized_question=metadata["normalized_question"],
                    content_sha256=content_sha,
                    result_revision=1,
                    evidence_status="needs_verification",
                    review_status="pending",
                ))
            elif saved.content_sha256 != content_sha:
                raise ValueError("persisted flywheel group result cannot be overwritten")

        for pair in pair_relations or []:
            left = rows.get(pair["source_id_a"])
            right = rows.get(pair["source_id_b"])
            if left is None or right is None or left.id == right.id:
                raise ValueError("pair relation references an unknown or duplicate source case")
            case_a = left.diagnosis_case_id or f"lcq:{left.id}"
            case_b = right.diagnosis_case_id or f"lcq:{right.id}"
            case_a, case_b = sorted((case_a, case_b))
            relation_id = hashlib.sha256(
                f"{pair['merge_task_id']}:{case_a}:{case_b}".encode("utf-8")).hexdigest()
            saved = await s.get(FlywheelPairRelation, relation_id)
            if saved is None:
                s.add(FlywheelPairRelation(
                    relation_id=relation_id,
                    merge_task_id=pair["merge_task_id"],
                    case_id_a=case_a, case_id_b=case_b,
                    relation_proposed=pair["relation_proposed"],
                    relation_final=None,
                    review_status=pair.get("review_status", "pending"),
                    review_source=pair.get("review_source"),
                    confirmation_source=None,
                    rationale=pair.get("rationale", ""),
                ))
            elif (saved.relation_proposed != pair["relation_proposed"]
                  or saved.case_id_a != case_a or saved.case_id_b != case_b):
                raise ValueError("persisted case-pair proposal cannot be overwritten")

        await s.commit()
        return stats


async def list_review_queue(status: str | None) -> list[ReviewQueue]:
    """审核页列表:按出现次数降序(频次=优先级),同频新的在前。"""
    async with db.async_session() as s:
        q = select(ReviewQueue).order_by(
            ReviewQueue.occurrence_count.desc(), ReviewQueue.id.desc())
        if status:
            q = q.where(ReviewQueue.review_status == status)
        return list((await s.execute(q)).scalars())


async def list_flywheel_group_results(canonical_question_id: str) -> list[dict]:
    async with db.async_session() as s:
        rows = (await s.execute(select(FlywheelGroupResult).where(
            FlywheelGroupResult.canonical_question_id == canonical_question_id
        ).order_by(FlywheelGroupResult.created_at, FlywheelGroupResult.group_result_id))).scalars()
        return [{
            "group_result_id": row.group_result_id,
            "merge_task_id": row.merge_task_id,
            "canonical_question_id": row.canonical_question_id,
            "member_case_ids": row.member_case_ids,
            "normalized_question": row.normalized_question,
            "result_revision": row.result_revision,
            "evidence_status": row.evidence_status,
            "review_status": row.review_status,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        } for row in rows]


async def list_flywheel_pair_relations(*, review_status: str | None = None,
                                       limit: int = 100, offset: int = 0) -> list[dict]:
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    async with db.async_session() as s:
        query = select(FlywheelPairRelation).order_by(
            FlywheelPairRelation.created_at, FlywheelPairRelation.relation_id)
        if review_status == "unreviewed":
            query = query.where(FlywheelPairRelation.relation_final.is_(None)) \
                .where(FlywheelPairRelation.review_status == "pending")
        elif review_status:
            query = query.where(FlywheelPairRelation.review_status == review_status)
        rows = list((await s.execute(query.limit(limit).offset(offset))).scalars())
        low_ids = {int(value[4:]) for row in rows for value in
                   (row.case_id_a, row.case_id_b) if value.startswith("lcq:")}
        diagnosis_ids = {value for row in rows for value in
                         (row.case_id_a, row.case_id_b) if not value.startswith("lcq:")}
        low_questions = {}
        if low_ids:
            low_questions = {f"lcq:{item.id}": item.raw_question for item in
                             (await s.execute(select(LowConfidenceQuestion).where(
                                 LowConfidenceQuestion.id.in_(low_ids)))).scalars()}
        diagnosis_questions = {}
        if diagnosis_ids:
            diagnosis_questions = {item.case_id: item.question for item in
                                   (await s.execute(select(DiagnosisCase).where(
                                       DiagnosisCase.case_id.in_(diagnosis_ids)))).scalars()}
        return [{
            "relation_id": row.relation_id,
            "merge_task_id": row.merge_task_id,
            "case_id_a": row.case_id_a,
            "case_id_b": row.case_id_b,
            "question_a": low_questions.get(row.case_id_a)
                or diagnosis_questions.get(row.case_id_a),
            "question_b": low_questions.get(row.case_id_b)
                or diagnosis_questions.get(row.case_id_b),
            "relation_proposed": row.relation_proposed,
            "relation_final": row.relation_final,
            "review_status": row.review_status,
            "review_source": row.review_source,
            "confirmation_source": row.confirmation_source,
            "rationale": row.rationale,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        } for row in rows]


async def get_flywheel_pair_relation(relation_id: str) -> dict | None:
    async with db.async_session() as s:
        row = await s.get(FlywheelPairRelation, relation_id)
        if row is None:
            return None
        questions = []
        for case_id in (row.case_id_a, row.case_id_b):
            if case_id.startswith("lcq:"):
                try:
                    source = await s.get(LowConfidenceQuestion, int(case_id[4:]))
                except ValueError:
                    source = None
                questions.append(source.raw_question if source else None)
            else:
                source = await s.scalar(select(DiagnosisCase).where(
                    DiagnosisCase.case_id == case_id))
                questions.append(source.question if source else None)
        return {
            "relation_id": row.relation_id,
            "merge_task_id": row.merge_task_id,
            "case_id_a": row.case_id_a, "case_id_b": row.case_id_b,
            "question_a": questions[0], "question_b": questions[1],
            "relation_proposed": row.relation_proposed,
            "relation_final": row.relation_final,
            "review_status": row.review_status,
            "review_source": row.review_source,
            "confirmation_source": row.confirmation_source,
            "rationale": row.rationale,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }


async def list_flywheel_pair_review_events(relation_id: str) -> list[dict]:
    async with db.async_session() as s:
        rows = (await s.execute(select(FlywheelPairReviewEvent).where(
            FlywheelPairReviewEvent.relation_id == relation_id
        ).order_by(FlywheelPairReviewEvent.created_at,
                   FlywheelPairReviewEvent.id))).scalars()
        return [{
            "decision": row.decision,
            "reviewer_id": row.reviewer_id,
            "comment": row.comment,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        } for row in rows]


async def finalize_flywheel_pair_relation(*, relation_id: str, decision: str,
                                          reviewer_id: str,
                                          comment: str | None = None) -> dict | None:
    if decision not in {"merge", "no_merge", "uncertain"}:
        raise ValueError("decision must be merge, no_merge, or uncertain")
    if not reviewer_id.strip():
        raise ValueError("reviewer_id is required")
    async with db.async_session() as s:
        row = await s.scalar(select(FlywheelPairRelation).where(
            FlywheelPairRelation.relation_id == relation_id).with_for_update())
        if row is None:
            return None
        row.relation_final = decision
        row.review_status = "confirmed" if decision == row.relation_proposed else "rejected"
        row.review_source = "human"
        row.confirmation_source = "human" if decision in {"merge", "no_merge"} else None
        s.add(FlywheelPairReviewEvent(
            relation_id=relation_id, decision=decision,
            reviewer_id=reviewer_id.strip(), comment=comment,
        ))
        await s.commit()
        return {
            "relation_id": row.relation_id,
            "relation_proposed": row.relation_proposed,
            "relation_final": row.relation_final,
            "review_status": row.review_status,
            "review_source": row.review_source,
            "confirmation_source": row.confirmation_source,
        }


async def get_flywheel_pair_metrics() -> dict:
    """Metrics on the human-audited pair sample only; do not extrapolate to all traffic."""
    async with db.async_session() as s:
        all_rows = list((await s.execute(select(
            FlywheelPairRelation.relation_proposed,
            FlywheelPairRelation.relation_final,
        ).where(FlywheelPairRelation.review_status != "withdrawn"))).all())
    rows = [(proposed, final) for proposed, final in all_rows if final is not None]
    tp = fp = fn = tn = abstain = 0
    final_uncertain = 0
    for proposed, final in rows:
        if proposed == "uncertain":
            abstain += 1
        if final == "uncertain":
            final_uncertain += 1
        if final == "merge":
            if proposed == "merge":
                tp += 1
            else:
                fn += 1
        elif final == "no_merge":
            if proposed == "merge":
                fp += 1
            elif proposed == "no_merge":
                tn += 1
    precision_denominator = tp + fp
    recall_denominator = tp + fn
    changed = sum(proposed != final for proposed, final in rows)
    return {
        "scope": "human_audited_pairs_only",
        "audited_pair_count": len(rows),
        "total_recorded_pairs": len(all_rows),
        "human_review_coverage": len(rows) / len(all_rows) if all_rows else None,
        "human_override_count": changed,
        "human_override_rate": changed / len(rows) if rows else None,
        "true_positive": tp, "false_positive": fp,
        "false_negative": fn, "true_negative": tn,
        "model_abstention_count": abstain,
        "final_uncertain_count": final_uncertain,
        "merge_precision": tp / precision_denominator if precision_denominator else None,
        "merge_recall": tp / recall_denominator if recall_denominator else None,
        "merge_f1": (2 * tp / (2 * tp + fp + fn)) if (2 * tp + fp + fn) else None,
        "warning": "Reviewed-sample metrics only; use documented sampling weights before estimating population rates.",
    }


async def get_review_detail(review_id: int) -> tuple[ReviewQueue, list[LowConfidenceQuestion]] | None:
    """详情:缺口行 + 归并进来的原话流水(带 source/快照,审核人判断真缺还是没检到)。"""
    async with db.async_session() as s:
        item = await s.get(ReviewQueue, review_id)
        if item is None:
            return None
        raws = list((await s.execute(
            select(LowConfidenceQuestion)
            .where(LowConfidenceQuestion.matched_review_id == review_id)
            .order_by(LowConfidenceQuestion.id)
        )).scalars())
        return item, raws


async def update_review_status(review_id: int, status: str, approved_answer: str | None = None) -> bool:
    """仅「待审」可流转(通过/驳回都是终态);返回是否真的更新了。"""
    async with db.async_session() as s:
        row = await s.get(ReviewQueue, review_id)
        if row is None or row.review_status != "待审":
            return False
        row.review_status = status
        if approved_answer is not None:
            row.approved_answer = approved_answer
        await s.commit()
        return True


async def delete_knowledge_chunks(ids: list[int]) -> None:
    """审核写回失败的回滚:删掉本次插入的 pending chunk,防重试造出重复知识条目。"""
    if not ids:
        return
    async with db.async_session() as s:
        for i in ids:
            row = await s.get(KnowledgeChunk, i)
            if row is not None:
                await s.delete(row)
        await s.commit()


async def insert_eval_run(triggered_by: str, dataset_size: int, metrics: dict) -> int:
    async with db.async_session() as s:
        row = EvalRun(triggered_by=triggered_by, dataset_size=dataset_size, metrics=metrics)
        s.add(row)
        await s.commit()
        return row.id


async def list_eval_runs(limit: int = 10) -> list[EvalRun]:
    async with db.async_session() as s:
        result = await s.execute(select(EvalRun).order_by(EvalRun.id.desc()).limit(limit))
        return list(result.scalars())


# ---- ch08 工具审计 ----


async def insert_tool_audit(
    conversation_id: int | None,
    tool_call_id: str | None,
    tool_name: str,
    tool_source: str,
    mcp_server: str | None,
    arguments: dict | None,
    result_summary: str | None,
    status: str,
    error_message: str | None,
    retry_count: int,
    duration_ms: int | None,
) -> None:
    """工具调用审计落一条。调用方(engine)自行 try/except——审计失败不许反拦工具执行。"""
    async with db.async_session() as s:
        s.add(ToolAuditLog(
            conversation_id=conversation_id, tool_call_id=tool_call_id,
            tool_name=tool_name, tool_source=tool_source, mcp_server=mcp_server,
            arguments=arguments, result_summary=result_summary, status=status,
            error_message=error_message, retry_count=retry_count, duration_ms=duration_ms,
        ))
        await s.commit()


# ---------- ch10 主题分类 ----------

def _pool_text_stmt():
    """池问题取文本的公共查询:优先归并后的标准化问法,没归并退回原话。"""
    return (
        select(LowConfidenceQuestion.id, LowConfidenceQuestion.raw_question,
               ReviewQueue.normalized_question)
        .outerjoin(ReviewQueue, LowConfidenceQuestion.matched_review_id == ReviewQueue.id)
        .order_by(LowConfidenceQuestion.id)
    )


async def list_pool_texts() -> list[dict]:
    """ch10 训练语料捞取:全量低置信度问题。"""
    async with db.async_session() as s:
        rows = (await s.execute(_pool_text_stmt())).all()
    return [{"question_id": qid, "text": norm or raw} for qid, raw, norm in rows]


async def list_unclassified_questions(limit: int = 500) -> list[dict]:
    """ch10 旁路批处理捞取:尚未归类、且已归并的低置信度问题。
    分类器只吃归并阶段产出的标准化问法;未归并(无 matched_review_id)的
    不进分类,留到下一轮归并后再归——保证输入永远是语义完整的单句,不喂原话。"""
    stmt = (
        _pool_text_stmt()
        .outerjoin(TopicClassification,
                   TopicClassification.question_id == LowConfidenceQuestion.id)
        .where(TopicClassification.id.is_(None))
        .where(LowConfidenceQuestion.matched_review_id.is_not(None))
        .limit(limit)
    )
    async with db.async_session() as s:
        rows = (await s.execute(stmt)).all()
    return [{"question_id": qid, "text": norm or raw} for qid, raw, norm in rows]


async def insert_topic_classifications(rows: list[dict]) -> int:
    """批量写归类结果;rows: [{question_id, labels}]。"""
    async with db.async_session() as s:
        s.add_all([TopicClassification(question_id=r["question_id"], labels=r["labels"])
                   for r in rows])
        await s.commit()
    return len(rows)


async def topic_distribution(samples_per_class: int = 3) -> dict:
    """主题分布:17 类各自问题量 + 每类样例;labels JSON 在 Python 侧聚合(量级小)。"""
    from app.core.taxonomy import TOPIC_NAMES

    stmt = (
        select(TopicClassification.labels, LowConfidenceQuestion.raw_question,
               ReviewQueue.normalized_question, TopicClassification.classified_at)
        .join(LowConfidenceQuestion,
              TopicClassification.question_id == LowConfidenceQuestion.id)
        .outerjoin(ReviewQueue, LowConfidenceQuestion.matched_review_id == ReviewQueue.id)
    )
    async with db.async_session() as s:
        rows = (await s.execute(stmt)).all()
    counts = {name: 0 for name in TOPIC_NAMES}
    samples: dict[str, list[str]] = {name: [] for name in TOPIC_NAMES}
    latest = None
    for labels, raw, norm, ts in rows:
        text = norm or raw
        latest = ts if latest is None or ts > latest else latest
        for lb in labels or []:
            if lb in counts:
                counts[lb] += 1
                # 样例去重:归并后同一句标准化问法会对应池里好几行,三条样例都一样就白给了
                if len(samples[lb]) < samples_per_class and text not in samples[lb]:
                    samples[lb].append(text)
    return {
        "total": len(rows),
        "latest": latest.isoformat() if latest else None,
        "classes": [{"label": n, "count": counts[n], "samples": samples[n]}
                    for n in TOPIC_NAMES],
    }


async def topic_questions(label: str, page: int = 1, size: int = 20) -> dict:
    """某一类目下已归类的问题,分页。总数与分布页那张图同源,都是数 labels 命中。

    labels 是 JSON 数组,筛选与分页都在 Python 侧做:归类总量是问题池规模(百级),
    一次全捞不贵,换来的是不依赖 MySQL 的 JSON 函数,sqlite 上跑测试也一致。
    """
    stmt = (
        select(TopicClassification.question_id, TopicClassification.labels,
               TopicClassification.classified_at, LowConfidenceQuestion.raw_question,
               LowConfidenceQuestion.source, LowConfidenceQuestion.created_at,
               ReviewQueue.normalized_question, ReviewQueue.occurrence_count,
               ReviewQueue.review_status)
        .join(LowConfidenceQuestion,
              TopicClassification.question_id == LowConfidenceQuestion.id)
        .outerjoin(ReviewQueue, LowConfidenceQuestion.matched_review_id == ReviewQueue.id)
        .order_by(TopicClassification.question_id.desc())
    )
    async with db.async_session() as s:
        rows = (await s.execute(stmt)).all()

    hit = [r for r in rows if label in (r[1] or [])]
    size = max(1, min(size, 100))
    pages = max(1, -(-len(hit) // size))
    page = max(1, min(page, pages))
    items = [
        {"question_id": qid, "labels": labels,
         "text": norm or raw, "raw_question": raw,
         "normalized": norm is not None,
         "source": source, "occurrence_count": occ,
         "review_status": status,
         "asked_at": asked.isoformat() if asked else None,
         "classified_at": ts.isoformat() if ts else None}
        for qid, labels, ts, raw, source, asked, norm, occ, status
        in hit[(page - 1) * size: page * size]
    ]
    return {"label": label, "total": len(hit), "page": page, "size": size,
            "pages": pages, "items": items}


# ---- ch04 编造个案台账 ----


async def upsert_faith_case(eval_id: str, bucket: str, query: str, answer: str, reason: str,
                            citations: list | None = None, strategy: str = "hybrid_rerank",
                            judge_model: str | None = None) -> tuple[int, bool]:
    """一题一行:已有就更新成最近一次并 seen_count+1,返回 (id, 是否复发)。

    复发 = 这条之前被标过「已解决 / 无需解决」,现在又被判编造。那不是新问题,而是上次
    没改对(或者改动被回退),所以状态退回「未解决」重新进待处理列表——resolved_at 留着,
    列表上据此打「复发」标。
    """
    async with db.async_session() as s:
        row = (await s.execute(
            select(FaithCase).where(FaithCase.eval_id == eval_id))).scalar_one_or_none()
        now = datetime.now()
        if row is None:
            row = FaithCase(eval_id=eval_id, bucket=bucket, query=query, answer=answer,
                            reason=reason, citations=citations, strategy=strategy,
                            judge_model=judge_model, status="未解决", seen_count=1,
                            first_seen_at=now, last_seen_at=now)
            s.add(row)
            await s.commit()
            return row.id, False
        reopened = row.status != "未解决"
        row.bucket, row.query, row.answer, row.reason = bucket, query, answer, reason
        row.strategy, row.judge_model = strategy, judge_model
        if citations is not None:
            row.citations = citations
        row.seen_count = (row.seen_count or 0) + 1
        row.last_seen_at = now
        row.status = "未解决"
        await s.commit()
        return row.id, reopened


async def list_faith_cases(status: str | None = None, page: int = 1,
                           size: int = 5) -> tuple[list[FaithCase], int, dict]:
    """分页列表 + 三个状态各自的条数。未解决排前面,同状态内最近判出的排前面。"""
    async with db.async_session() as s:
        counts = {k: 0 for k in ("未解决", "已解决", "无需解决")}
        for st, n in (await s.execute(
                select(FaithCase.status, func.count()).group_by(FaithCase.status))).all():
            counts[st] = int(n)
        q = select(FaithCase)
        if status:
            q = q.where(FaithCase.status == status)
        total = int((await s.execute(
            select(func.count()).select_from(q.subquery()))).scalar_one())
        rows = (await s.execute(
            q.order_by(case((FaithCase.status == "未解决", 0), else_=1),
                       FaithCase.last_seen_at.desc(), FaithCase.id.desc())
            .limit(size).offset(max(0, (page - 1) * size)))).scalars()
        return list(rows), total, counts


async def faith_case_status_map() -> dict[str, str]:
    """eval_id → 处置状态。给幻觉率算「本轮」用:报告只给这一轮判出了哪几题,
    每题现在是什么处置得回台账查——台账是跨轮累计的,不能直接拿它的总数当本轮判出。"""
    async with db.async_session() as s:
        rows = (await s.execute(select(FaithCase.eval_id, FaithCase.status))).all()
        return {eid: st for eid, st in rows}


async def set_faith_case_status(case_id: int, status: str,
                                resolution: str | None = None) -> FaithCase | None:
    """人工处置:已解决 / 无需解决 / 退回未解决。

    处置要留交代:标「已解决」得写清怎么解决的,标「无需解决」得写清为什么不用改——
    半年后回头看,一个没写理由的「无需解决」和没处理过没区别。校验在 API 层(能回 400),
    这里只负责落库:退回未解决时连 resolved_at 和说明一起清掉。
    """
    async with db.async_session() as s:
        row = await s.get(FaithCase, case_id)
        if row is None:
            return None
        row.status = status
        if status == "未解决":
            row.resolved_at, row.resolution = None, None
        else:
            row.resolved_at = datetime.now()
            row.resolution = resolution
        await s.commit()
        await s.refresh(row)
        return row
