from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import diagnosis


def test_list_diagnosis_cases(monkeypatch):
    row = SimpleNamespace(case_id="case-1", trace_id="f" * 32, conversation_id=4,
                          question="能退吗", intent="商品咨询", failure_type="unknown",
                          predicted_failure_type="unknown", human_failure_type=None,
                          diagnosis_status="pending", diagnosis_reason="需人工检查",
                          review_queue_id=None,
                          retrieved_docs=[], evidence_confidence=0.2,
                          selfcheck_result=None, tool_status=None, final_answer="请转人工",
                          user_feedback=None, config_version=None, created_at=None)

    async def fake_list(status, limit, failure_type=None, offset=0):
        assert status == "pending" and limit == 10 and failure_type is None and offset == 0
        return [row]

    monkeypatch.setattr(diagnosis.repository, "list_diagnosis_cases", fake_list)
    app = FastAPI()
    app.include_router(diagnosis.router)
    response = TestClient(app).get("/api/diagnosis/cases?status=pending&limit=10")
    assert response.status_code == 200
    assert response.json()["items"][0]["case_id"] == "case-1"
    assert response.json()["items"][0]["trace_id"] == "f" * 32


def test_list_diagnosis_cases_filters_by_failure_type(monkeypatch):
    seen = {}

    async def fake_list(status, limit, failure_type=None, offset=0):
        seen.update(status=status, limit=limit, failure_type=failure_type, offset=offset)
        return []

    monkeypatch.setattr(diagnosis.repository, "list_diagnosis_cases", fake_list)
    app = FastAPI()
    app.include_router(diagnosis.router)
    response = TestClient(app).get("/api/diagnosis/cases?failure_type=tool_error")
    assert response.status_code == 200
    assert seen == {"status": None, "limit": 50, "failure_type": "tool_error", "offset": 0}


def test_list_diagnosis_cases_rejects_unknown_failure_type():
    app = FastAPI()
    app.include_router(diagnosis.router)
    response = TestClient(app).get("/api/diagnosis/cases?failure_type=made_up")
    assert response.status_code == 400


def test_diagnosis_workload_exposes_category_metrics(monkeypatch):
    expected = {"categories": [{"failure_type": "knowledge_gap", "raw_cases": 12,
                                "review_clusters": 3, "deduplication_rate": 0.75}],
                "totals": {"raw_cases": 12, "review_clusters": 3}}

    async def fake_summary(): return expected
    monkeypatch.setattr(diagnosis.repository, "diagnosis_review_workload", fake_summary)
    monkeypatch.setattr(diagnosis.diagnosis_review, "load_policy", lambda: {
        "policy_version": "v1", "categories": {"knowledge_gap": {"sample_rate": 1.0}},
        "risk_escalation": {"always_review_failure_types": [],
                            "evidence_confidence_below": 0.26},
    })
    app = FastAPI()
    app.include_router(diagnosis.router)
    response = TestClient(app).get("/api/diagnosis/cases/workload")
    assert response.status_code == 200
    result = response.json()
    assert result["review_policy_version"] == "v1"
    assert result["categories"][0]["audit_sample_rate"] == 1.0
    assert result["categories"][0]["review_clusters"] == 3


def test_review_sample_returns_configured_risk_escalations(monkeypatch):
    row = SimpleNamespace(case_id="unknown-case", trace_id=None, conversation_id=4,
                          question="q", intent=None, failure_type="unknown",
                          predicted_failure_type="unknown", human_failure_type=None,
                          diagnosis_status="pending", diagnosis_reason="need review",
                          review_queue_id=None, retrieved_docs=[], evidence_confidence=0.9,
                          selfcheck_result=None, tool_status=None, final_answer="a",
                          user_feedback=None, config_version=None, created_at=None)
    seen = {}
    async def fake_list(status, limit, failure_type=None, offset=0):
        seen.update(status=status, limit=limit, failure_type=failure_type, offset=offset)
        return [row]
    monkeypatch.setattr(diagnosis.repository, "list_diagnosis_cases", fake_list)
    monkeypatch.setattr(diagnosis.diagnosis_review, "load_policy", lambda: {
        "policy_version": "v-test", "categories": {"unknown": {"sample_rate": 0.0}},
        "risk_escalation": {"always_review_failure_types": ["unknown"]},
    })
    app = FastAPI()
    app.include_router(diagnosis.router)
    response = TestClient(app).get("/api/diagnosis/cases/review-sample?failure_type=unknown")
    assert response.status_code == 200
    assert response.json()["items"][0]["review_decision"]["reason"] == "risk_escalation"
    assert seen == {"status": "pending", "limit": 50, "failure_type": "unknown", "offset": 0}


def test_update_diagnosis_status_validates_failure_type(client=None):
    app = FastAPI()
    app.include_router(diagnosis.router)
    response = TestClient(app).post("/api/diagnosis/cases/case-1/status", json={
        "failure_type": "made_up", "diagnosis_status": "confirmed", "diagnosis_reason": "wrong"})
    assert response.status_code == 422
