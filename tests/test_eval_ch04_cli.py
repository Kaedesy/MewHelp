import json
from types import SimpleNamespace

import pytest
from scripts import eval_ch04
from app.core.optimization_candidates import RetrievalCandidate


def test_load_reads_explicit_utf8_dataset(tmp_path):
    dataset = tmp_path / "eval.jsonl"
    rows = [{"id": "C1", "bucket": "C_colloquial", "query": "口语问题？"},
            {"id": "D1", "bucket": "D_absent", "query": "资料未说明的问题？",
             "expected_handling": "说明当前资料未提及，并建议核实。"}]
    dataset.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
                       encoding="utf-8")

    assert eval_ch04._load(dataset) == rows


def test_generation_samples_includes_cases_with_expected_handling():
    rows = [
        {"id": "A1", "bucket": "A_policy"},
        {"id": "D1", "bucket": "D_absent", "expected_handling": "说明未知项并建议核实"},
        {"id": "D2", "bucket": "D_absent"},
    ]

    assert [row["id"] for row in eval_ch04._generation_samples(rows)] == ["A1", "D1"]


def test_write_report_honors_custom_output_paths(tmp_path):
    report_path = tmp_path / "answer-eval.json"
    log_path = tmp_path / "answer-eval.txt"

    eval_ch04._write_report({"r": 1}, {"c": 1}, {"g": 1}, {"meta": True},
                            {"note": "n"}, report_path, log_path)

    assert json.loads(report_path.read_text(encoding="utf-8")) == {
        "meta": {"meta": True}, "retrieval": {"r": 1},
        "evidence_coverage": {"c": 1}, "generation": {"g": 1},
        "read_notes": {"note": "n"},
    }
    assert log_path.exists()


@pytest.mark.asyncio
async def test_gen_one_records_expected_handling_judgment(monkeypatch):
    async def fake_try(factory, label):
        if label.startswith("gen:"):
            return SimpleNamespace(content="积分可兑换实物；转赠政策未说明，建议咨询客服。")
        if label.startswith("faith:"):
            return SimpleNamespace(faithful=True, reason="事实均有证据支持")
        if label.startswith("handling:"):
            return SimpleNamespace(compliant=True, unsupported_claims=False,
                                   reason="已说明未知项并建议核实")
        raise AssertionError(label)

    monkeypatch.setattr(eval_ch04, "_try", fake_try)
    sample = {"id": "C1", "bucket": "C_colloquial", "query": "能转赠积分吗？",
              "expect_points": [], "expected_handling": "说明转赠政策未提及并建议核实"}
    hits = {"hybrid_rerank": {"C1": [{"question": "积分怎么用", "answer": "可兑换实物"}]}}

    result = await eval_ch04._gen_one(
        "hybrid_rerank", sample, hits, object(), object(), object(), object(),
        object(), object(), object())

    assert result["handling_compliant"] is True
    assert result["handling_unsupported_claims"] is False
    assert result["handling_reason"] == "已说明未知项并建议核实"
    assert result["answer"].startswith("积分可兑换实物")


@pytest.mark.asyncio
async def test_candidate_mode_passes_candidate_parameters_to_retrieval(monkeypatch):
    seen = {}

    async def fake_search(query, **kwargs):
        seen.update(query=query, **kwargs)
        return []

    monkeypatch.setattr(eval_ch04.retrieval, "search_knowledge", fake_search)
    monkeypatch.setattr(eval_ch04, "CANDIDATE_CONFIGS", {
        "recall_20": RetrievalCandidate("recall_20", 20, 5),
    })

    await eval_ch04._retrieve("recall_20", {"query": "question"})

    assert seen == {"query": "question", "strategy": "hybrid_rerank",
                    "top_k": 5, "recall_top_k": 20, "split": True}


@pytest.mark.asyncio
async def test_retrieve_retries_transient_rate_limit(monkeypatch):
    calls = 0

    async def fake_search(query, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            error = RuntimeError("rate limited")
            error.response = SimpleNamespace(status_code=429)
            raise error
        return [{"question": "ok"}]

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(eval_ch04.retrieval, "search_knowledge", fake_search)
    monkeypatch.setattr(eval_ch04.asyncio, "sleep", no_wait)

    result = await eval_ch04._retrieve("hybrid_rerank", {"query": "question"})

    assert calls == 2
    assert result == [{"question": "ok"}]
