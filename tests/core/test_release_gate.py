import hashlib
import json
import math

from app.core.release_gate import evaluate_gate


BUCKETS = ["A_policy", "B_model", "C_colloquial", "E_multi", "D_absent"]


def _report(candidate_mrr=0.8, status="complete", role="test"):
    buckets = {name: {"expected": 10, "evaluated": 10,
                      "recall_at_5": 0.9, "mrr": 0.8} for name in BUCKETS[:-1]}
    buckets["D_absent"] = {"expected": 10, "evaluated": 10,
                           "confidence_gate_refusal_rate": 0.95}
    dataset_hash = "d" * 64
    provenance = {"dataset_role": "test", "dataset_sha256": dataset_hash,
                  "derived_from_existing_eval_data": False, "previously_evaluated": False,
                  "used_for_candidate_tuning": False, "curation_method": "manual",
                  "curator_id": "reviewer-1", "source_references": ["policy-doc-1"]}
    return {
        "meta": {"dataset_role": role, "dataset_sha256": dataset_hash, "knowledge_version": "kb-v1",
                 "knowledge_source_sha256": "a" * 64, "code_revision": "rev1",
                 "models": {"embed": "e", "rerank": "r", "chat": "c"},
                 "dataset_provenance": provenance,
                 "dataset_provenance_sha256": hashlib.sha256(json.dumps(
                     provenance, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                 ).encode("utf-8")).hexdigest()},
        "candidates": {
            "baseline": {"status": "complete", "expected_samples": 50,
                         "evaluated_samples": 50, "metrics": {"recall_at_5": 0.9, "mrr": 0.8,
                             "confidence_gate_refusal_rate": 0.95,
                             "confidence_gate_false_refusal_rate": 0.02},
                         "buckets": buckets},
            "variant": {"status": status, "expected_samples": 50,
                        "evaluated_samples": 50, "metrics": {"recall_at_5": 0.9, "mrr": candidate_mrr,
                            "confidence_gate_refusal_rate": 0.95,
                            "confidence_gate_false_refusal_rate": 0.02},
                        "buckets": buckets},
        },
    }


def _thresholds(max_regression=0.02):
    return {"min_samples": 30, "required_buckets": list(BUCKETS),
            "calibration": {"baseline_runs": 3, "dataset_role": "validation",
                            "dataset_sha256": "b" * 64,
                            "knowledge_version": "kb-v1", "knowledge_source_sha256": "a" * 64,
                            "code_revision": "rev1",
                            "models": {"embed": "e", "rerank": "r", "chat": "c"}},
            "metrics": {"recall_at_5": {"max_regression": max_regression},
                        "mrr": {"max_regression": max_regression},
                        "confidence_gate_refusal_rate": {"min_value": 0.9},
                        "confidence_gate_false_refusal_rate": {"max_value": 0.05}},
            "bucket_metrics": {
                **{b: {"recall_at_5": {"max_regression": max_regression}}
                   for b in BUCKETS[:-1]},
                "D_absent": {"confidence_gate_refusal_rate": {"min_value": 0.9}},
            }}


def _answer_report(report=None, handling_count=10):
    meta = (report or _report())["meta"]
    config = {"name": "variant", "recall_top_k": 50, "rerank_top_k": 5}
    cases = [{"strat": "variant", "id": f"C{i}", "answer": "ok", "faithful": True}
             for i in range(50)]
    handling = [{"strat": "variant", "id": f"D{i}", "handling_compliant": True,
                 "handling_unsupported_claims": False} for i in range(handling_count)]
    return {
        "meta": {"dataset_role": meta["dataset_role"],
                 "dataset_sha256": meta["dataset_sha256"],
                 "dataset_provenance_sha256": meta["dataset_provenance_sha256"],
                 "knowledge_version": meta["knowledge_version"],
                 "knowledge_source_sha256": meta["knowledge_source_sha256"],
                 "code_revision": meta["code_revision"], "models": meta["models"],
                 "candidate_configs": {"variant": config},
                 "generation_sample_count": 50,
                 "expected_handling_counts": {"D_absent": 10}},
        "generation": {"skipped": 0, "answer_cases": cases, "handling_cases": handling,
                       "answer_coverage": {"variant": {"overall": 0.95}},
                       "faithfulness_by_candidate": {"variant": {
                           bucket: {"v": 0.98, "answered": 10} for bucket in BUCKETS}},
                       },
    }


def _quality_thresholds():
    thresholds = _thresholds()
    thresholds["require_answer_quality"] = True
    thresholds["answer_quality"] = {"metrics": {
        "answer_coverage": {"min_value": 0.9},
        "faithfulness": {"min_value": 0.95},
        "handling_compliance": {"min_value": 0.9},
        "unsupported_claim_rate": {"max_value": 0.05},
    }}
    return thresholds


def test_gate_passes_complete_report_with_configured_thresholds():
    out = evaluate_gate(_report(), "variant", _thresholds())
    assert out["status"] == "PASS"


def test_gate_fails_when_metric_exceeds_allowed_regression():
    out = evaluate_gate(_report(candidate_mrr=0.7), "variant", _thresholds())
    assert out["status"] == "FAIL"
    assert any("mrr" in reason for reason in out["reasons"])


def test_gate_fails_on_known_regression_even_when_other_operator_limits_are_missing():
    thresholds = _thresholds()
    thresholds.pop("min_samples")
    thresholds["metrics"]["confidence_gate_refusal_rate"].pop("min_value")
    thresholds["metrics"]["confidence_gate_false_refusal_rate"].pop("max_value")
    thresholds["bucket_metrics"]["D_absent"]["confidence_gate_refusal_rate"].pop("min_value")
    out = evaluate_gate(_report(candidate_mrr=0.7), "variant", thresholds)
    assert out["status"] == "FAIL"
    assert any("mrr" in reason for reason in out["reasons"])


def test_gate_keeps_data_integrity_problems_inconclusive_even_when_metric_fails():
    out = evaluate_gate(_report(candidate_mrr=0.7, role="validation"), "variant", _thresholds())
    assert out["status"] == "INCONCLUSIVE"


def test_gate_is_inconclusive_when_using_validation_dataset():
    out = evaluate_gate(_report(role="validation"), "variant", _thresholds())
    assert out["status"] == "INCONCLUSIVE"


def test_gate_is_inconclusive_when_thresholds_are_not_calibrated():
    out = evaluate_gate(_report(), "variant", {"min_samples": 1, "metrics": {}})
    assert out["status"] == "INCONCLUSIVE"


def test_gate_requires_operator_absolute_safety_limits():
    thresholds = _thresholds()
    thresholds["metrics"]["confidence_gate_refusal_rate"].pop("min_value")
    thresholds["metrics"]["confidence_gate_false_refusal_rate"].pop("max_value")
    thresholds["bucket_metrics"]["D_absent"]["confidence_gate_refusal_rate"].pop("min_value")
    out = evaluate_gate(_report(), "variant", thresholds)
    assert out["status"] == "INCONCLUSIVE"
    assert any("绝对" in reason for reason in out["reasons"])


def test_gate_is_inconclusive_when_evaluation_was_incomplete():
    out = evaluate_gate(_report(status="incomplete"), "variant", _thresholds())
    assert out["status"] == "INCONCLUSIVE"


def test_gate_is_inconclusive_when_calibration_does_not_match_test_environment():
    thresholds = _thresholds()
    thresholds["calibration"]["knowledge_version"] = "stale-kb"
    out = evaluate_gate(_report(), "variant", thresholds)
    assert out["status"] == "INCONCLUSIVE"
    assert any("knowledge_version" in reason for reason in out["reasons"])


def test_gate_rejects_reusing_calibration_dataset_as_final_test():
    report = _report()
    thresholds = _thresholds()
    thresholds["calibration"]["dataset_sha256"] = _report()["meta"]["dataset_sha256"]
    out = evaluate_gate(report, "variant", thresholds)
    assert out["status"] == "INCONCLUSIVE"
    assert any("不能与阈值校准集相同" in reason for reason in out["reasons"])


def test_gate_requires_all_defined_evaluation_buckets():
    thresholds = _thresholds()
    thresholds["required_buckets"].remove("D_absent")
    out = evaluate_gate(_report(), "variant", thresholds)
    assert out["status"] == "INCONCLUSIVE"
    assert any("缺少必要评估分桶" in reason for reason in out["reasons"])


def test_gate_requires_bucket_counts_to_cover_dataset():
    report = _report()
    report["candidates"]["variant"]["buckets"]["A_policy"]["expected"] = 9
    report["candidates"]["variant"]["buckets"]["A_policy"]["evaluated"] = 9
    out = evaluate_gate(report, "variant", _thresholds())
    assert out["status"] == "INCONCLUSIVE"
    assert any("样本数之和与总样本数不一致" in reason for reason in out["reasons"])


def test_gate_rejects_boolean_sample_threshold():
    thresholds = _thresholds()
    thresholds["min_samples"] = True
    out = evaluate_gate(_report(), "variant", thresholds)
    assert out["status"] == "INCONCLUSIVE"
    assert any("有效 min_samples" in reason for reason in out["reasons"])


def test_gate_requires_independent_unseen_manual_test_provenance():
    report = _report()
    report["meta"].pop("dataset_provenance")
    report["meta"].pop("dataset_provenance_sha256")
    out = evaluate_gate(report, "variant", _thresholds())
    assert out["status"] == "INCONCLUSIVE"
    assert any("独立性来源清单" in reason for reason in out["reasons"])


def test_gate_rejects_test_data_derived_from_previous_evaluation_set():
    report = _report()
    provenance = report["meta"]["dataset_provenance"]
    provenance["derived_from_existing_eval_data"] = True
    report["meta"]["dataset_provenance_sha256"] = hashlib.sha256(json.dumps(
        provenance, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    out = evaluate_gate(report, "variant", _thresholds())
    assert out["status"] == "INCONCLUSIVE"
    assert any("独立于既有评估数据" in reason for reason in out["reasons"])


def test_gate_rejects_non_finite_or_out_of_range_metric_values():
    report = _report()
    report["candidates"]["variant"]["metrics"]["mrr"] = math.nan
    out = evaluate_gate(report, "variant", _thresholds())
    assert out["status"] == "INCONCLUSIVE"
    assert any("有限数值" in reason for reason in out["reasons"])


def test_gate_rejects_tampered_test_provenance_hash():
    report = _report()
    report["meta"]["dataset_provenance_sha256"] = "tampered"
    out = evaluate_gate(report, "variant", _thresholds())
    assert out["status"] == "INCONCLUSIVE"
    assert any("来源清单哈希无效" in reason for reason in out["reasons"])


def test_gate_requires_and_scores_answer_quality_report():
    report = _report()
    report["candidates"]["variant"]["config"] = {
        "name": "variant", "recall_top_k": 50, "rerank_top_k": 5,
    }
    out = evaluate_gate(report, "variant", _quality_thresholds(),
                        answer_report=_answer_report(report))
    assert out["status"] == "PASS"
    assert {c["metric"] for c in out["checks"]} >= {
        "answer_coverage", "faithfulness", "handling_compliance", "unsupported_claim_rate",
    }


def test_gate_requires_answer_report_when_answer_quality_is_enabled():
    out = evaluate_gate(_report(), "variant", _quality_thresholds())
    assert out["status"] == "INCONCLUSIVE"
    assert any("未提供答复评估报告" in reason for reason in out["reasons"])


def test_gate_rejects_mismatched_or_incomplete_answer_quality_report():
    report = _report()
    report["candidates"]["variant"]["config"] = {
        "name": "variant", "recall_top_k": 50, "rerank_top_k": 5,
    }
    answer_report = _answer_report(report, handling_count=9)
    answer_report["meta"]["knowledge_version"] = "other-kb"
    out = evaluate_gate(report, "variant", _quality_thresholds(), answer_report)
    assert out["status"] == "INCONCLUSIVE"
    assert any("版本不一致：knowledge_version" in reason for reason in out["reasons"])
    assert any("处理方式裁判样本不完整" in reason for reason in out["reasons"])


def test_gate_fails_when_absolute_answer_quality_limit_is_violated():
    report = _report()
    report["candidates"]["variant"]["config"] = {
        "name": "variant", "recall_top_k": 50, "rerank_top_k": 5,
    }
    answer_report = _answer_report(report)
    answer_report["generation"]["answer_coverage"]["variant"]["overall"] = 0.85
    out = evaluate_gate(report, "variant", _quality_thresholds(), answer_report)
    assert out["status"] == "FAIL"
    assert any("answer_coverage" in reason for reason in out["reasons"])
