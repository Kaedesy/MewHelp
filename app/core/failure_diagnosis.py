"""从客服请求中捕获可观察的失败信号，并保存证据关联的诊断案例。

低置信度本身不能证明检索失败或知识缺口；这类判断需要更多证据或人工确认。
"""
from __future__ import annotations

import logging
import hashlib
import json

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


def _subtask_id(case_id: str, scope: str, evidence_refs: list[dict]) -> str:
    evidence_hash = hashlib.sha256(json.dumps(
        evidence_refs, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    return hashlib.sha256(
        f"{case_id}:capture-v1:{scope}:{evidence_hash}".encode("utf-8")).hexdigest()


def _fault_id(task_id: str, fault_type: str, evidence_refs: list[dict]) -> str:
    stable = json.dumps(evidence_refs, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"))
    return hashlib.sha256(f"{task_id}:{fault_type}:{stable}".encode("utf-8")).hexdigest()


def _diagnostic_tasks(state: dict, case_id: str | None,
                      user_feedback: str | None) -> tuple[list[dict], str]:
    """Split independently observable evidence into scoped, multi-fault records."""
    if not case_id:
        return [], "in_progress"
    trace = state.get("trace") or {}
    tool_calls = trace.get("tool_calls") or []
    tasks = []

    # 工具调用、证据闸门和用户反馈分别建子任务，允许一条请求记录多个问题。
    if tool_calls:
        all_tool_evidence = [{
            "source": "tool_audit", "call_index": index,
            "tool_call_id": item.get("tool_call_id"),
            "tool_name": item.get("tool_name"), "status": item.get("status"),
        } for index, item in enumerate(tool_calls)]
        task_id = _subtask_id(case_id, "tool_execution", all_tool_evidence)
        failed = [item for item in tool_calls if item.get("status") in {"失败", "超时"}]
        faults = []
        for index, item in enumerate(tool_calls):
            if item not in failed:
                continue
            evidence = [{
                "source": "tool_audit",
                "call_index": index,
                "tool_call_id": item.get("tool_call_id"),
                "tool_name": item.get("tool_name"),
                "status": item.get("status"),
            }]
            faults.append({
                "fault_id": _fault_id(task_id, "tool_error", evidence),
                "fault_type": "tool_error", "status": "confirmed",
                "proposal_source": "rule", "review_source": None,
                "confirmation_source": "rule", "evidence_refs": evidence,
                "reason": "Tool audit record contains an explicit failed or timed-out execution.",
            })
        tasks.append({
            "task_id": task_id, "diagnostic_scope": "tool_execution",
            "execution_status": "completed",
            "outcome_proposed": "faults_found" if failed else "no_fault_found",
            "outcome_final": "faults_found" if failed else "no_fault_found",
            "evidence_refs": all_tool_evidence,
            "faults": faults,
        })

    fallback = state.get("fallback_source")
    if fallback in {"retrieval_low_conf", "self_check"}:
        evidence = [{"source": "fallback_gate", "fallback_source": fallback}]
        task_id = _subtask_id(case_id, "answer_evidence", evidence)
        tasks.append({
            "task_id": task_id, "diagnostic_scope": "answer_evidence",
            "execution_status": "completed", "outcome_proposed": "faults_found",
            "outcome_final": "faults_found", "evidence_refs": evidence,
            "faults": [{
                "fault_id": _fault_id(task_id, "evidence_insufficient", evidence),
                "fault_type": "evidence_insufficient", "status": "confirmed",
                "proposal_source": "rule", "review_source": None,
                "confirmation_source": "rule", "evidence_refs": evidence,
                "reason": "A deterministic evidence gate prevented an unsupported answer.",
            }],
        })

    if user_feedback == "down":
        evidence = [{"source": "user_feedback", "value": "down"}]
        tasks.append({
            "task_id": _subtask_id(case_id, "user_feedback", evidence),
            "diagnostic_scope": "user_feedback", "execution_status": "completed",
            "outcome_proposed": "inconclusive", "outcome_final": "inconclusive",
            "evidence_refs": evidence, "faults": [],
        })

    has_confirmed = any(
        fault["status"] == "confirmed" for task in tasks for fault in task["faults"])
    unresolved = any(task["outcome_final"] is None for task in tasks)
    has_inconclusive = any(task["outcome_final"] == "inconclusive" for task in tasks)
    if unresolved:
        case_status = "partially_diagnosed" if has_confirmed else "in_progress"
    elif has_confirmed and has_inconclusive:
        case_status = "partially_diagnosed"
    elif has_confirmed:
        case_status = "faults_found"
    elif has_inconclusive:
        case_status = "inconclusive"
    else:
        case_status = "no_fault_found"
    return tasks, case_status


def diagnose(state: dict, *, user_feedback: str | None = None,
             trace_id: str | None = None) -> dict | None:
    """仅对明确异常或用户点踩生成诊断记录，不把普通请求都送入诊断池。"""
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

    case_id = state.get("case_id")
    diagnosis_tasks, case_diagnosis_status = _diagnostic_tasks(state, case_id, user_feedback)
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
        "case_diagnosis_status": case_diagnosis_status,
        "_diagnosis_tasks": diagnosis_tasks,
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
    # 诊断记录写库失败只记日志，不影响已经生成的客服回复。
    case = diagnose(state, user_feedback=user_feedback, trace_id=trace_id)
    if case is None or not case["case_id"] or not case["question"]:
        return None
    try:
        return await repository.upsert_diagnosis_case(case)
    except Exception:
        # Persistence/observability must not turn a completed answer into a failed request.
        logger.exception("failed to save diagnosis case case_id=%s", case["case_id"])
        return None
