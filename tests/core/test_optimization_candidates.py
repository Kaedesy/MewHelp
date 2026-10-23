import pytest

from app.core.optimization_candidates import RetrievalCandidate, default_candidates, parse_candidate


def test_default_candidates_include_baseline_and_single_parameter_variants():
    candidates = default_candidates()
    assert len(candidates) >= 3
    assert candidates[0].name == "baseline"
    assert candidates[1].recall_top_k != candidates[0].recall_top_k
    assert candidates[2].rerank_top_k != candidates[0].rerank_top_k


@pytest.mark.parametrize("payload", [
    {"name": "", "recall_top_k": 10, "rerank_top_k": 5},
    {"name": "bad", "recall_top_k": 0, "rerank_top_k": 1},
    {"name": "bad", "recall_top_k": 10, "rerank_top_k": 11},
    {"name": "bad", "recall_top_k": 10, "rerank_top_k": 2, "strategy": "vector"},
])
def test_invalid_candidate_is_rejected(payload):
    with pytest.raises(ValueError):
        parse_candidate(payload)


def test_candidate_is_immutable_and_serializable():
    candidate = RetrievalCandidate("small", 20, 5).validate()
    assert candidate.to_dict()["recall_top_k"] == 20


@pytest.mark.asyncio
async def test_candidate_evaluator_reports_retrieval_and_refusal_proxy(monkeypatch):
    from scripts import eval_candidates

    async def fake_search(query, **kwargs):
        if query == "absent":
            return []
        return [{"id": 1, "section_path": "退货政策", "question": "退货政策",
                 "answer": "7天内可退", "rerank_score": 0.9}]

    monkeypatch.setattr(eval_candidates.retrieval, "search_knowledge", fake_search)
    samples = [
        {"id": "A1", "bucket": "A_policy", "query": "退货政策是什么",
         "expect_section": ["退货政策"], "expect_points": ["7天"]},
        {"id": "D1", "bucket": "D_absent", "query": "absent", "should_refuse": True,
         "expected_handling": "说明资料未提及，不要断言支持或不支持。"},
    ]
    result = await eval_candidates._score_candidate(samples, RetrievalCandidate("c", 20, 5))
    assert result["status"] == "complete"
    assert result["metrics"]["recall_at_5"] == 1.0
    assert result["metrics"]["confidence_gate_refusal_rate"] == 1.0
    assert len(result["sample_results"]) == 2
    assert result["sample_results"][0]["metrics"]["mrr"] == 1.0
    assert result["sample_results"][1]["expected_refusal"] is True
    assert result["sample_results"][1]["expected_handling"] == samples[1]["expected_handling"]


@pytest.mark.asyncio
async def test_candidate_evaluator_retries_transient_rate_limits(monkeypatch):
    from scripts import eval_candidates

    class RateLimitError(Exception):
        status_code = 429

    calls = 0
    async def fake_search(query, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RateLimitError("retry")
        return []

    async def no_sleep(_):
        return None

    monkeypatch.setattr(eval_candidates.retrieval, "search_knowledge", fake_search)
    monkeypatch.setattr(eval_candidates.asyncio, "sleep", no_sleep)
    result = await eval_candidates._score_candidate(
        [{"id": "D1", "bucket": "D_absent", "query": "absent", "should_refuse": True}],
        RetrievalCandidate("retry", 20, 5),
    )
    assert result["status"] == "complete"
    assert calls == 2


def test_candidate_report_lists_per_question_metric_changes():
    from scripts.eval_candidates import sample_differences

    baseline = {"sample_results": [
        {"id": "A1", "bucket": "A_policy", "status": "complete",
         "metrics": {"recall_at_5": 1.0, "mrr": 1.0}},
        {"id": "B1", "bucket": "B_model", "status": "complete",
         "metrics": {"recall_at_5": 1.0, "mrr": 0.5}},
    ]}
    candidate = {"sample_results": [
        {"id": "A1", "bucket": "A_policy", "status": "complete",
         "metrics": {"recall_at_5": 1.0, "mrr": 1.0}},
        {"id": "B1", "bucket": "B_model", "status": "complete",
         "metrics": {"recall_at_5": 0.0, "mrr": 0.0}},
    ]}
    changes = sample_differences(baseline, candidate)
    assert len(changes) == 1
    assert changes[0]["id"] == "B1"
    assert changes[0]["baseline"]["recall_at_5"] == 1.0
    assert changes[0]["candidate"]["recall_at_5"] == 0.0
