"""Conservative, evidence-linked diagnosis for suspicious customer-support turns.

Only observable failures are classified automatically. Low confidence does not
prove a retrieval miss or knowledge gap; those labels require human evidence.
"""
from __future__ import annotations

import logging

from app.db import repository
from app.config import settings

logger = logging.getLogger(__name__)

FAILURE_TYPES = {
    "retrieval_miss", "rerank_error", "evidence_insufficient", "generation_error",
    "tool_error", "routing_error", "knowledge_gap", "unknown",
}
DIAGNOSIS_STATUSES = {"pending", "confirmed", "false_positive", "insufficient_data"}


def _question(state: dict) -> str:
    for msg in reversed(state.get("messages") or []):
        if getattr(msg, "type", "") == "human":
            content = getattr(msg, "content", "")
            return content if isinstance(content, str) else ""
    return ""


def _documents(state: dict) -> list[dict]:
    ranked = state.get("retrieved_hits") or []
    if ranked:
        return ranked
    snapshot = state.get("retrieved_snapshot") or []
    by_id = {item.get("id"): item for item in snapshot if item.get("id") is not None}
    docs = []
    for rank, citation in enumerate(state.get("citations") or [], start=1):
        doc_id = citation.get("id")
        snap = by_id.get(doc_id, {})
        docs.append({"id": doc_id, "rank": rank, "rerank_score": snap.get("rerank_score"),
                     "section_path": citation.get("section_path"),
                     "question": citation.get("question"), "answer": citation.get("answer")})
    return docs or snapshot


def diagnose(state: dict, *, user_feedback: str | None = None,
             trace_id: str | None = None) -> dict | None:
    """Return a case payload only for suspicious turns or explicit negative feedback."""
    trace = state.get("trace") or {}
    tool_calls = trace.get("tool_calls") or []
    failed_tools = [item for item in tool_calls if item.get("status") in {"失败", "超时"}]
    fallback = state.get("fallback_source")

    if failed_tools:
        failure_type = "tool_error"
        status = "pending"
        reason = "工具执行记录包含失败或超时；需人工检查工具结果及用户最终答复。"
    elif fallback in {"retrieval_low_conf", "self_check"}:
        failure_type = "evidence_insufficient"
        status = "pending"
        reason = (f"系统因 {fallback} 触发拒答。该信号只说明当前证据未通过回答闸，"
                  "不能单独证明检索失败或知识库缺口。")
    elif user_feedback == "down":
        failure_type = "unknown"
        status = "insufficient_data"
        reason = "用户反馈未解决；反馈本身不证明事实错误，需结合答案和证据人工判断。"
    else:
        return None

    if user_feedback == "down" and failure_type != "unknown":
        reason += " 用户同时反馈未解决。"

    return {
        "case_id": state.get("case_id"),
        "trace_id": trace_id or state.get("trace_id"),
        "conversation_id": state.get("conversation_id"),
        "question": _question(state),
        "intent": state.get("intent"),
        "failure_type": failure_type,
        "predicted_failure_type": failure_type,
        "human_failure_type": None,
        "diagnosis_status": status,
        "diagnosis_reason": reason,
        "retrieved_docs": _documents(state) or None,
        "evidence_confidence": state.get("evidence_confidence"),
        "selfcheck_result": ({"reason": trace.get("self_check")}
                             if trace.get("self_check") else None),
        "tool_status": tool_calls or None,
        "final_answer": state.get("answer") or _assistant_answer(state),
        "user_feedback": user_feedback,
        "config_version": (f"retrieval-v1:recall={settings.recall_top_k}:"
                           f"rerank={settings.rerank_top_k}:"
                           f"confidence={settings.evidence_confidence_threshold}"),
    }


def _assistant_answer(state: dict) -> str:
    for msg in reversed(state.get("messages") or []):
        if getattr(msg, "type", "") == "ai":
            content = getattr(msg, "content", "")
            if isinstance(content, str) and content:
                return content
    return ""


async def capture(state: dict, *, user_feedback: str | None = None,
                  trace_id: str | None = None) -> dict | None:
    case = diagnose(state, user_feedback=user_feedback, trace_id=trace_id)
    if case is None or not case["case_id"] or not case["question"]:
        return None
    try:
        return await repository.upsert_diagnosis_case(case)
    except Exception:
        # Persistence/observability must not turn a completed answer into a failed request.
        logger.exception("failed to save diagnosis case case_id=%s", case["case_id"])
        return None
