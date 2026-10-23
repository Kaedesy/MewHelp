import pytest

from app.core import flywheel
from app.core.flywheel import NormalizeResult
from app.db import repository


@pytest.mark.asyncio
async def test_diagnosis_case_upsert_and_human_confirmation_survive_later_feedback(
        db_session_factory, monkeypatch):
        row = await repository.upsert_diagnosis_case({
            "case_id": "case-1", "trace_id": "f" * 32, "conversation_id": None,
            "question": "能退吗", "intent": "商品咨询", "failure_type": "evidence_insufficient",
            "predicted_failure_type": "evidence_insufficient", "human_failure_type": None,
            "diagnosis_status": "pending", "diagnosis_reason": "低置信拒答",
            "retrieved_docs": [{"id": 7, "rank": 1}], "evidence_confidence": 0.2,
            "selfcheck_result": None, "tool_status": None, "final_answer": "请转人工",
            "user_feedback": None, "config_version": "retrieval-v1",
        })
        assert row.case_id == "case-1"
        assert row.trace_id == "f" * 32
        confirmed = await repository.update_diagnosis_case(
            "case-1", failure_type="knowledge_gap", diagnosis_status="confirmed",
            diagnosis_reason="人工找到标准知识来源")
        assert confirmed.diagnosis_status == "confirmed"
        assert confirmed.predicted_failure_type == "evidence_insufficient"
        assert confirmed.human_failure_type == "knowledge_gap"
        assert confirmed.review_queue_id is None

        calls = []

        async def normalize(raw_question, candidates):
            calls.append(raw_question)
            return NormalizeResult(normalized_question=raw_question,
                                   matched_question_id=None,
                                   ai_suggested_answer="示例答案，仅供审核参考")

        monkeypatch.setattr(flywheel, "normalize_and_match", normalize)
        stats = await flywheel.process_pending()
        assert stats == {"processed": 1, "merged": 0, "created": 1, "skipped": 0}
        assert calls == ["能退吗"]
        linked = await repository.get_diagnosis_case("case-1")
        assert linked.review_queue_id is not None
        assert linked.trace_id == "f" * 32
        workload = await repository.diagnosis_review_workload()
        gap_stats = next(x for x in workload["categories"]
                         if x["failure_type"] == "knowledge_gap")
        assert gap_stats["raw_cases"] == 1
        assert gap_stats["review_clusters"] == 1
        assert gap_stats["open_review_clusters"] == 1
        assert gap_stats["deduplication_rate"] == 0.0
        review, raws = await repository.get_review_detail(linked.review_queue_id)
        assert review.review_status == "待审"
        assert len(raws) == 1
        assert raws[0].diagnosis_case_id == "case-1"
        assert raws[0].source == "diagnosis_confirmed"

        # Reconfirming the same case must not create duplicate flywheel records.
        await repository.update_diagnosis_case(
            "case-1", failure_type="knowledge_gap", diagnosis_status="confirmed",
            diagnosis_reason="人工复核仍确认")
        assert await repository.fetch_unmatched_low_conf(10) == []

        again = await repository.upsert_diagnosis_case({
            "case_id": "case-1", "trace_id": None, "conversation_id": None,
            "question": "能退吗", "intent": "商品咨询", "failure_type": "unknown",
            "predicted_failure_type": "unknown", "human_failure_type": None,
            "diagnosis_status": "insufficient_data", "diagnosis_reason": "后来点踩",
            "retrieved_docs": None, "evidence_confidence": 0.2,
            "selfcheck_result": None, "tool_status": None, "final_answer": "请转人工",
            "user_feedback": "down", "config_version": "retrieval-v1",
        })
        assert again.failure_type == "knowledge_gap"
        assert again.diagnosis_status == "confirmed"
        assert again.diagnosis_reason == "人工复核仍确认"
        assert again.predicted_failure_type == "evidence_insufficient"
        assert again.human_failure_type == "knowledge_gap"
        assert again.user_feedback == "down"
        summary = await repository.diagnosis_label_summary()
        assert summary["labeled_confirmed"] == 1
        assert summary["type_matches"] == 0
        assert summary["type_agreement"] == 0.0
