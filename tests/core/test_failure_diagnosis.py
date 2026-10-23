from langchain_core.messages import AIMessage, HumanMessage

from app.core.failure_diagnosis import diagnose


def _state(**values):
    return {"case_id": "c-1", "conversation_id": 2,
            "messages": [HumanMessage("能退吗"), AIMessage("请转人工")], **values}


def test_low_confidence_is_evidence_insufficient_not_knowledge_gap():
    case = diagnose(_state(fallback_source="retrieval_low_conf", evidence_confidence=0.1,
                           retrieved_snapshot=[{"id": 9, "rerank_score": 0.1}],
                           trace={"confidence_signals": {"top1_score": 0.1}}))
    assert case["failure_type"] == "evidence_insufficient"
    assert case["diagnosis_status"] == "pending"
    assert "不能单独证明" in case["diagnosis_reason"]
    assert case["retrieved_docs"][0]["id"] == 9


def test_negative_feedback_alone_stays_unknown():
    case = diagnose(_state(), user_feedback="down")
    assert case["failure_type"] == "unknown"
    assert case["diagnosis_status"] == "insufficient_data"


def test_failed_tool_is_diagnosed_from_recorded_status():
    case = diagnose(_state(trace={"tool_calls": [{"name": "query_order", "status": "超时"}]}))
    assert case["failure_type"] == "tool_error"
    assert case["tool_status"][0]["status"] == "超时"


def test_successful_turn_without_negative_feedback_does_not_create_case():
    assert diagnose(_state()) is None


def test_diagnosis_case_retains_explicit_trace_id():
    case = diagnose(_state(fallback_source="retrieval_low_conf"), trace_id="a" * 32)
    assert case["trace_id"] == "a" * 32
