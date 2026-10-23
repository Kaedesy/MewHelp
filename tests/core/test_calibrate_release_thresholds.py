import copy

import pytest

from scripts.calibrate_release_thresholds import calibrate_reports, observe_baseline_variability


BUCKETS = ["A_policy", "B_model", "C_colloquial", "E_multi", "D_absent"]


def _report(delta):
    buckets = {name: {"expected": 40, "evaluated": 40,
                      "recall_at_5": 0.8 + delta, "mrr": 0.7 + delta}
               for name in BUCKETS[:-1]}
    buckets["D_absent"] = {"expected": 40, "evaluated": 40,
                           "confidence_gate_refusal_rate": 0.9 + delta}
    return {
        "meta": {"dataset_role": "validation", "dataset_sha256": "validation-sha",
                 "knowledge_version": "kb-v1", "knowledge_source_sha256": "kb-sha",
                 "code_revision": "rev1",
                 "models": {"embed": "e", "rerank": "r", "chat": "c"}},
        "candidates": {"baseline": {
            "config": {"recall_top_k": 50, "rerank_top_k": 10},
            "status": "complete", "expected_samples": 200, "evaluated_samples": 200,
            "metrics": {"recall_at_5": 0.8 + delta, "mrr": 0.7 + delta,
                        "confidence_gate_refusal_rate": 0.9 + delta,
                        "confidence_gate_false_refusal_rate": 0.03 + delta / 10},
            "buckets": buckets,
        }},
    }


def test_calibration_uses_repeated_baseline_deviation_and_records_provenance():
    out = calibrate_reports([_report(-0.01), _report(0), _report(0.01)],
                            min_samples=30, min_absent_refusal_rate=0.9,
                            max_false_refusal_rate=0.05)
    assert out["calibration"]["baseline_runs"] == 3
    assert out["calibration"]["dataset_sha256"] == "validation-sha"
    assert out["metrics"]["recall_at_5"]["max_regression"] == 0.01
    assert out["metrics"]["confidence_gate_refusal_rate"]["min_value"] == 0.9
    assert out["metrics"]["confidence_gate_false_refusal_rate"]["max_value"] == 0.05


def test_calibration_requires_three_repeated_baselines():
    with pytest.raises(ValueError, match="至少需要 3 次"):
        calibrate_reports([_report(0), _report(0)], min_samples=30,
                          min_absent_refusal_rate=0.9, max_false_refusal_rate=0.05)


def test_calibration_rejects_mixed_knowledge_versions():
    second = copy.deepcopy(_report(0.01))
    second["meta"]["knowledge_version"] = "kb-v2"
    with pytest.raises(ValueError, match="knowledge_version"):
        calibrate_reports([_report(-0.01), _report(0), second], min_samples=30,
                          min_absent_refusal_rate=0.9, max_false_refusal_rate=0.05)


def test_observed_only_calibration_does_not_invent_safety_limits():
    out = observe_baseline_variability([_report(-0.01), _report(0), _report(0.01)])
    assert out["calibration"]["baseline_runs"] == 3
    assert out["calibration"]["operator_safety_limits"]["status"] == "pending_operator_input"
    assert "min_samples" not in out
    assert "min_value" not in out["metrics"]["confidence_gate_refusal_rate"]
    assert "max_value" not in out["metrics"]["confidence_gate_false_refusal_rate"]
