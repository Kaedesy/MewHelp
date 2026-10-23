"""Calibrate release tolerances from repeated, complete validation baselines.

This script only derives observed run-to-run tolerances. Operators must supply
the independent minimum sample count and absolute refusal safety thresholds.
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import statistics
import sys

METRICS = {
    "recall_at_5": "max_regression",
    "mrr": "max_regression",
    "confidence_gate_refusal_rate": "max_regression",
    "confidence_gate_false_refusal_rate": "max_increase",
}
BUCKET_METRICS = {
    "A_policy": {"recall_at_5": "max_regression"},
    "B_model": {"recall_at_5": "max_regression"},
    "C_colloquial": {"recall_at_5": "max_regression"},
    "E_multi": {"recall_at_5": "max_regression"},
    "D_absent": {"confidence_gate_refusal_rate": "max_regression"},
}
MATCH_META = ("knowledge_version", "knowledge_source_sha256", "code_revision", "models")


def _value(value, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"缺少数值指标: {path}")
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"指标必须是 0 到 1 的有限数: {path}")
    return float(value)


def _tolerance(values: list[float]) -> tuple[float, float]:
    median = statistics.median(values)
    return median, max(abs(value - median) for value in values)


def calibrate_reports(reports: list[dict], *, min_samples: int,
                      min_absent_refusal_rate: float,
                      max_false_refusal_rate: float) -> dict:
    if len(reports) < 3:
        raise ValueError("至少需要 3 次完整基线运行才能校准")
    if min_samples < 1:
        raise ValueError("min_samples 必须大于 0")
    for name, value in (("min_absent_refusal_rate", min_absent_refusal_rate),
                        ("max_false_refusal_rate", max_false_refusal_rate)):
        if not 0 <= value <= 1:
            raise ValueError(f"{name} 必须在 0 到 1 之间")

    metas, baselines = [], []
    for index, report in enumerate(reports, start=1):
        meta = report.get("meta") or {}
        if meta.get("dataset_role") != "validation":
            raise ValueError(f"第 {index} 份基线报告不是 validation 数据集")
        for key in ("dataset_sha256", *MATCH_META):
            if not meta.get(key):
                raise ValueError(f"第 {index} 份基线报告缺少元数据: {key}")
        baseline = (report.get("candidates") or {}).get("baseline") or {}
        if baseline.get("status") != "complete":
            raise ValueError(f"第 {index} 份基线运行不完整")
        if baseline.get("evaluated_samples") != baseline.get("expected_samples"):
            raise ValueError(f"第 {index} 份基线样本不完整")
        metas.append(meta)
        baselines.append(baseline)

    reference_meta = metas[0]
    for index, meta in enumerate(metas[1:], start=2):
        for key in ("dataset_sha256", *MATCH_META):
            if meta.get(key) != reference_meta.get(key):
                raise ValueError(f"第 {index} 份基线的 {key} 与其它运行不一致")
    if len({baseline.get("expected_samples") for baseline in baselines}) != 1:
        raise ValueError("重复基线的预期样本数不一致")
    if baselines[0].get("expected_samples", 0) < min_samples:
        raise ValueError("基线样本数低于指定的 min_samples")
    configs = [baseline.get("config") for baseline in baselines]
    if any(config != configs[0] for config in configs[1:]):
        raise ValueError("重复基线配置不一致")

    metric_values: dict[str, list[float]] = {name: [] for name in METRICS}
    bucket_values = {bucket: {metric: [] for metric in rules}
                     for bucket, rules in BUCKET_METRICS.items()}
    for index, baseline in enumerate(baselines, start=1):
        for metric in METRICS:
            metric_values[metric].append(_value(
                (baseline.get("metrics") or {}).get(metric), f"run{index}.{metric}"))
        buckets = baseline.get("buckets") or {}
        for bucket, rules in BUCKET_METRICS.items():
            values = buckets.get(bucket) or {}
            if values.get("expected") != values.get("evaluated"):
                raise ValueError(f"第 {index} 份基线分桶样本不完整: {bucket}")
            for metric in rules:
                bucket_values[bucket][metric].append(_value(
                    values.get(metric), f"run{index}.{bucket}.{metric}"))

    metrics, observed = {}, {"overall": {}}
    for metric, direction in METRICS.items():
        median, tolerance = _tolerance(metric_values[metric])
        rule = {direction: round(tolerance, 6)}
        if metric == "confidence_gate_refusal_rate":
            rule["min_value"] = min_absent_refusal_rate
        if metric == "confidence_gate_false_refusal_rate":
            rule["max_value"] = max_false_refusal_rate
        metrics[metric] = rule
        observed["overall"][metric] = {
            "median": round(median, 6), "max_absolute_deviation": round(tolerance, 6),
            "runs": [round(value, 6) for value in metric_values[metric]],
        }

    bucket_metrics = {}
    for bucket, rules in BUCKET_METRICS.items():
        bucket_metrics[bucket] = {}
        observed[bucket] = {}
        for metric, direction in rules.items():
            median, tolerance = _tolerance(bucket_values[bucket][metric])
            rule = {direction: round(tolerance, 6)}
            if bucket == "D_absent":
                rule["min_value"] = min_absent_refusal_rate
            bucket_metrics[bucket][metric] = rule
            observed[bucket][metric] = {
                "median": round(median, 6),
                "max_absolute_deviation": round(tolerance, 6),
                "runs": [round(value, 6) for value in bucket_values[bucket][metric]],
            }

    return {
        "schema_version": 1,
        "min_samples": min_samples,
        "require_answer_quality": True,
        "answer_quality": {
            "metrics": {
                "answer_coverage": {"min_value": None},
                "faithfulness": {"min_value": None},
                "handling_compliance": {"min_value": None},
                "unsupported_claim_rate": {"max_value": None},
            },
            "note": "Fill operator-approved absolute limits after reviewing answer-quality judge calibration.",
        },
        "required_buckets": list(BUCKET_METRICS),
        "metrics": metrics,
        "bucket_metrics": bucket_metrics,
        "calibration": {
            "baseline_runs": len(reports), "dataset_role": "validation",
            "dataset_sha256": reference_meta["dataset_sha256"],
            **{key: reference_meta[key] for key in MATCH_META},
            "method": "median with maximum observed absolute deviation across repeated baselines",
            "observed": observed,
            "operator_safety_limits": {
                "min_absent_refusal_rate": min_absent_refusal_rate,
                "max_false_refusal_rate": max_false_refusal_rate,
            },
        },
    }


def observe_baseline_variability(reports: list[dict]) -> dict:
    """Record repeated-run noise without inventing operator safety limits."""
    result = calibrate_reports(
        reports, min_samples=1, min_absent_refusal_rate=0.0, max_false_refusal_rate=1.0,
    )
    result.pop("min_samples", None)
    result["calibration"]["operator_safety_limits"] = {
        "status": "pending_operator_input",
        "required": ["min_samples", "min_absent_refusal_rate", "max_false_refusal_rate"],
    }
    for metric in ("confidence_gate_refusal_rate", "confidence_gate_false_refusal_rate"):
        result["metrics"][metric].pop("min_value", None)
        result["metrics"][metric].pop("max_value", None)
    result["bucket_metrics"]["D_absent"]["confidence_gate_refusal_rate"].pop("min_value", None)
    result["bucket_metrics"]["D_absent"]["confidence_gate_refusal_rate"].pop("max_value", None)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=pathlib.Path, action="append", required=True,
                        help="validation baseline report; repeat at least 3 times")
    parser.add_argument("--out", type=pathlib.Path, required=True)
    parser.add_argument("--observed-only", action="store_true",
                        help="record baseline variance only; leave safety limits unset")
    parser.add_argument("--min-samples", type=int,
                        help="minimum complete sample count required in final test run")
    parser.add_argument("--min-absent-refusal-rate", type=float,
                        help="operator-defined minimum for unanswerable-question bucket")
    parser.add_argument("--max-false-refusal-rate", type=float,
                        help="operator-defined maximum for answerable-question bucket")
    args = parser.parse_args()
    try:
        reports = [json.loads(path.read_text(encoding="utf-8")) for path in args.report]
        limits = (args.min_samples, args.min_absent_refusal_rate, args.max_false_refusal_rate)
        if args.observed_only:
            if any(value is not None for value in limits):
                parser.error("--observed-only cannot be combined with operator threshold values")
            result = observe_baseline_variability(reports)
        else:
            if any(value is None for value in limits):
                parser.error("provide all three operator thresholds or use --observed-only")
            result = calibrate_reports(
                reports, min_samples=args.min_samples,
                min_absent_refusal_rate=args.min_absent_refusal_rate,
                max_false_refusal_rate=args.max_false_refusal_rate,
            )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"校准失败: {exc}", file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    if args.observed_only:
        print(f"基线波动记录已写入 {args.out}")
    else:
        print(f"阈值已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
