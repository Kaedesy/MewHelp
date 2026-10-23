"""Read and human-confirm automatically collected failure cases."""
from fastapi import APIRouter, HTTPException, Query

from app.core.failure_diagnosis import DIAGNOSIS_STATUSES, FAILURE_TYPES
from app.core import diagnosis_review, evolution_proposals
from app.db import repository
from app.schemas.diagnosis import DiagnosisUpdate

router = APIRouter(prefix="/api/diagnosis/cases")


def _out(row) -> dict:
    return {
        "case_id": row.case_id, "trace_id": row.trace_id,
        "conversation_id": row.conversation_id, "question": row.question,
        "intent": row.intent, "failure_type": row.failure_type,
        "predicted_failure_type": row.predicted_failure_type,
        "human_failure_type": row.human_failure_type,
        "diagnosis_status": row.diagnosis_status, "diagnosis_reason": row.diagnosis_reason,
        "review_queue_id": row.review_queue_id,
        "retrieved_docs": row.retrieved_docs,
        "evidence_confidence": row.evidence_confidence,
        "selfcheck_result": row.selfcheck_result, "tool_status": row.tool_status,
        "final_answer": row.final_answer, "user_feedback": row.user_feedback,
        "config_version": row.config_version,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("")
async def list_cases(status: str | None = None,
                     failure_type: str | None = None,
                     limit: int = Query(50, ge=1, le=200),
                     offset: int = Query(0, ge=0)) -> dict:
    if status is not None and status not in DIAGNOSIS_STATUSES | {"pending"}:
        raise HTTPException(status_code=400, detail="无效的诊断状态")
    if failure_type is not None and failure_type not in FAILURE_TYPES:
        raise HTTPException(status_code=400, detail="鏃犳晥鐨勫け璐ョ被鍒?")
    rows = await repository.list_diagnosis_cases(status, limit, failure_type, offset)
    return {"items": [_out(row) for row in rows]}


@router.get("/review-sample")
async def review_sample(failure_type: str | None = None,
                        limit: int = Query(50, ge=1, le=200),
                        offset: int = Query(0, ge=0)) -> dict:
    """Return a stable category sample; explicit risk escalations bypass sampling."""
    if failure_type is not None and failure_type not in FAILURE_TYPES:
        raise HTTPException(status_code=400, detail="鏃犳晥鐨勫け璐ョ被鍒?")
    policy = diagnosis_review.load_policy()
    scan_limit = limit
    rows = await repository.list_diagnosis_cases("pending", scan_limit, failure_type, offset)
    selected = []
    for row in rows:
        item = _out(row)
        decision = diagnosis_review.review_decision(item, policy)
        if decision["selected"]:
            item["review_decision"] = decision
            selected.append(item)
    return {
        "policy_version": policy["policy_version"], "failure_type": failure_type,
        "items": selected[:limit], "scanned_cases": len(rows),
        "next_offset": offset + len(rows) if len(rows) == scan_limit else None,
    }


@router.get("/summary")
async def diagnosis_summary() -> dict:
    return await repository.diagnosis_label_summary()


@router.get("/workload")
async def diagnosis_workload() -> dict:
    """Show case volume, review coverage, and savings from the existing dedup queue."""
    report = await repository.diagnosis_review_workload()
    policy = diagnosis_review.load_policy()
    by_type = {item["failure_type"]: item for item in report["categories"]}
    categories = []
    always_review = (policy.get("risk_escalation") or {}).get("always_review_failure_types", [])
    for failure_type, rule in policy["categories"].items():
        item = by_type.get(failure_type, {
            "failure_type": failure_type, "raw_cases": 0, "pending_diagnosis": 0,
            "human_reviewed": 0, "linked_cases": 0, "review_clusters": 0,
            "open_review_clusters": 0, "cluster_occurrences": 0,
            "deduplication_rate": None, "human_review_coverage": None,
        })
        item["audit_sample_rate"] = rule["sample_rate"]
        item["risk_escalation_always"] = failure_type in always_review
        item["risk_escalation_confidence_threshold"] = (
            (policy.get("risk_escalation") or {}).get("evidence_confidence_below"))
        categories.append(item)
    report["categories"] = categories
    report["review_policy_version"] = policy["policy_version"]
    return report


@router.get("/evolution-proposals")
async def diagnosis_evolution_proposals(
    min_cases: int = Query(evolution_proposals.MIN_CASES_PER_PROPOSAL, ge=1, le=50),
) -> dict:
    """Suggest bounded offline candidates from human-confirmed case categories."""
    cases = []
    offset = 0
    page_size = 500
    while True:
        page = await repository.list_diagnosis_cases(
            "confirmed", limit=page_size, offset=offset)
        cases.extend(_out(row) for row in page)
        if len(page) < page_size:
            break
        offset += len(page)
    return evolution_proposals.propose_from_confirmed_cases(cases, min_cases=min_cases)


@router.get("/{case_id}")
async def get_case(case_id: str) -> dict:
    row = await repository.get_diagnosis_case(case_id)
    if row is None:
        raise HTTPException(status_code=404, detail="案例不存在")
    return _out(row)


@router.post("/{case_id}/status")
async def update_case(case_id: str, req: DiagnosisUpdate) -> dict:
    row = await repository.update_diagnosis_case(
        case_id, failure_type=req.failure_type,
        diagnosis_status=req.diagnosis_status, diagnosis_reason=req.diagnosis_reason)
    if row is None:
        raise HTTPException(status_code=404, detail="案例不存在")
    return _out(row)
