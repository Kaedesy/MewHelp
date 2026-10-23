from app.core.diagnosis_review import load_policy, review_decision


def test_versioned_review_policy_loads_all_failure_categories():
    policy = load_policy()
    assert policy["policy_version"]
    assert set(policy["categories"]) == {
        "retrieval_miss", "rerank_error", "evidence_insufficient", "generation_error",
        "tool_error", "routing_error", "knowledge_gap", "unknown",
    }


def test_sampling_is_stable_and_zero_rate_selects_nothing():
    case = {"case_id": "case-123", "failure_type": "retrieval_miss",
            "evidence_confidence": 0.9}
    policy = {"policy_version": "test-v1",
              "categories": {"retrieval_miss": {"sample_rate": 0.5}},
              "risk_escalation": {}}
    assert review_decision(case, policy) == review_decision(case, policy)
    policy["categories"]["retrieval_miss"]["sample_rate"] = 0.0
    result = review_decision(case, policy)
    assert result["selected"] is False
    assert result["reason"] == "outside_sample"


def test_risk_escalation_bypasses_sampling_rate():
    policy = {"policy_version": "test-v1",
              "categories": {"unknown": {"sample_rate": 0.0},
                             "retrieval_miss": {"sample_rate": 0.0}},
              "risk_escalation": {"always_review_failure_types": ["unknown"],
                                  "evidence_confidence_below": 0.2}}
    assert review_decision({"case_id": "a", "failure_type": "unknown"}, policy)["selected"]
    low = review_decision({"case_id": "b", "failure_type": "retrieval_miss",
                           "evidence_confidence": 0.1}, policy)
    assert low["selected"] is True
    assert low["reason"] == "low_evidence_confidence"
