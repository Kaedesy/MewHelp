"""Pure comparison logic for an offline retrieval release gate."""
from __future__ import annotations

import math
import hashlib
import json

REQUIRED_METRICS = {
    "recall_at_5", "mrr", "confidence_gate_refusal_rate",
    "confidence_gate_false_refusal_rate",
}
REQUIRED_BUCKETS = {
    "A_policy", "B_model", "C_colloquial", "D_absent", "E_multi",
}
REQUIRED_ANSWER_QUALITY_METRICS = {
    "answer_coverage", "faithfulness", "handling_compliance", "unsupported_claim_rate",
}


def _finite_number(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _is_sha256(value) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(char in "0123456789abcdef" for char in value.lower()))


def _check_metric(name: str, before, after, rule: dict) -> tuple[dict | None, str | None]:
    if not _finite_number(before) or not _finite_number(after):
        return None, f"门禁指标缺失或不是有限数值：{name}"
    if not 0 <= before <= 1 or not 0 <= after <= 1:
        return None, f"门禁指标超出 0 到 1 范围：{name}"
    has_regression = "max_regression" in rule
    has_increase = "max_increase" in rule
    has_min = "min_value" in rule
    has_max = "max_value" in rule
    if has_regression and has_increase:
        return None, f"{name} 不能同时设置 max_regression 和 max_increase"
    if not (has_regression or has_increase or has_min or has_max):
        return None, f"{name} 未设置相对或绝对门槛"
    if has_regression and not _finite_number(rule["max_regression"]):
        return None, f"{name} 缺少有效 max_regression"
    if has_increase and not _finite_number(rule["max_increase"]):
        return None, f"{name} 缺少有效 max_increase"
    if has_regression:
        allowed = rule["max_regression"]
        if not 0 <= allowed <= 1:
            return None, f"{name} 的允许退化幅度必须在 0 到 1 之间"
        passed = after >= before - allowed
        check = {"metric": name, "baseline": before, "candidate": after,
                 "max_regression": allowed, "passed": passed}
    elif has_increase:
        allowed = rule["max_increase"]
        if not 0 <= allowed <= 1:
            return None, f"{name} 的允许增长幅度必须在 0 到 1 之间"
        passed = after <= before + allowed
        check = {"metric": name, "baseline": before, "candidate": after,
                 "max_increase": allowed, "passed": passed}
    else:
        passed = True
        check = {"metric": name, "baseline": before, "candidate": after, "passed": True}
    if "min_value" in rule:
        minimum = rule["min_value"]
        if not _finite_number(minimum):
            return None, f"{name} 缺少有效 min_value"
        if not 0 <= minimum <= 1:
            return None, f"{name} 的 min_value 必须在 0 到 1 之间"
        check["min_value"] = minimum
        passed = passed and after >= minimum
    if "max_value" in rule:
        maximum = rule["max_value"]
        if not _finite_number(maximum):
            return None, f"{name} 缺少有效 max_value"
        if not 0 <= maximum <= 1:
            return None, f"{name} 的 max_value 必须在 0 到 1 之间"
        if "min_value" in check and check["min_value"] > maximum:
            return None, f"{name} 的 min_value 不能大于 max_value"
        check["max_value"] = maximum
        passed = passed and after <= maximum
    check["passed"] = passed
    return check, None


def _answer_quality_values(answer_report: dict, candidate_name: str) -> tuple[dict, list[str]]:
    reasons = []
    meta = answer_report.get("meta") or {}
    generation = answer_report.get("generation") or {}
    expected = meta.get("generation_sample_count")
    candidate_cases = [row for row in generation.get("answer_cases", [])
                       if row.get("strat") == candidate_name]
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
        reasons.append("答复评估报告缺少有效 generation_sample_count")
    elif len(candidate_cases) != expected:
        reasons.append(f"答复评估样本不完整：{candidate_name} {len(candidate_cases)}/{expected}")
    elif len({row.get("id") for row in candidate_cases}) != expected:
        reasons.append(f"答复评估样本 ID 重复或缺失：{candidate_name}")
    if any(not row.get("answer") or row.get("faithful") is None for row in candidate_cases):
        reasons.append(f"答复或 Faithfulness 评估缺失：{candidate_name}")
    if generation.get("skipped", 0) != 0:
        reasons.append("答复评估存在失败或跳过的裁判调用")

    answer_coverage = ((generation.get("answer_coverage") or {}).get(candidate_name) or {}).get("overall")
    faith_buckets = ((generation.get("faithfulness_by_candidate") or {}).get(candidate_name) or {})
    faith_count = sum((row or {}).get("answered", 0) for row in faith_buckets.values())
    faith_value = (sum((row or {}).get("v", 0) * (row or {}).get("answered", 0)
                       for row in faith_buckets.values()) / faith_count) if faith_count else None

    expected_handling = sum((meta.get("expected_handling_counts") or {}).values())
    handling_cases = [row for row in generation.get("handling_cases", [])
                      if row.get("strat") == candidate_name]
    if expected_handling and len(handling_cases) != expected_handling:
        reasons.append(f"处理方式裁判样本不完整：{candidate_name} "
                       f"{len(handling_cases)}/{expected_handling}")
    if any(row.get("handling_compliant") is None
           or row.get("handling_unsupported_claims") is None for row in handling_cases):
        reasons.append(f"处理方式裁判结果缺失：{candidate_name}")
    handling_value = (sum(bool(row.get("handling_compliant")) for row in handling_cases)
                      / len(handling_cases)) if handling_cases else None
    unsupported_value = (sum(bool(row.get("handling_unsupported_claims")) for row in handling_cases)
                         / len(handling_cases)) if handling_cases else None
    return {
        "answer_coverage": answer_coverage,
        "faithfulness": faith_value if faith_count == expected else None,
        "handling_compliance": handling_value if handling_cases else None,
        "unsupported_claim_rate": unsupported_value if handling_cases else None,
    }, reasons


def _is_threshold_configuration_reason(reason: str) -> bool:
    """Whether a reason is an unset/invalid operator threshold, not bad evidence."""
    prefixes = (
        "尚未基于基线波动配置指标退化阈值",
        "缺少必要门禁指标阈值：",
        "门槛未设置有效 min_samples",
        "confidence_gate_refusal_rate 缺少运营方定义的绝对拒答下限",
        "confidence_gate_false_refusal_rate 缺少运营方定义的绝对误拒上限",
        "缺少必要答案质量门槛：",
        "answer_coverage 缺少运营方定义的最低质量线",
        "faithfulness 缺少运营方定义的最低质量线",
        "handling_compliance 缺少运营方定义的最低质量线",
        "unsupported_claim_rate 缺少运营方定义的最高比例",
        "D_absent 拒答率缺少运营方定义的绝对下限",
        "未配置分桶退化门槛：",
    )
    if reason.startswith(prefixes):
        return True
    return any(fragment in reason for fragment in (
        "缺少有效 max_regression", "缺少有效 max_increase",
        "缺少有效 min_value", "缺少有效 max_value",
        "不能同时设置 max_regression 和 max_increase",
        "未设置相对或绝对门槛",
        "允许退化幅度必须在 0 到 1 之间",
        "允许增长幅度必须在 0 到 1 之间",
        "的 min_value 必须在 0 到 1 之间",
        "的 max_value 必须在 0 到 1 之间",
        "的 min_value 不能大于 max_value",
    ))


def evaluate_gate(report: dict, candidate_name: str, thresholds: dict,
                  answer_report: dict | None = None) -> dict:
    candidates = report.get("candidates") or {}
    baseline = candidates.get("baseline")
    candidate = candidates.get(candidate_name)
    if not baseline or not candidate:
        return {"status": "INCONCLUSIVE", "reasons": ["基线或候选报告缺失"], "checks": []}

    reasons = []
    meta = report.get("meta") or {}
    if meta.get("dataset_role") != "test":
        reasons.append("最终门禁必须使用标记为 test 的数据集")
    required_meta = ("dataset_sha256", "knowledge_version", "knowledge_source_sha256",
                     "code_revision", "models")
    for key in required_meta:
        if not meta.get(key):
            reasons.append(f"运行元数据缺少 {key}")
    for key in ("dataset_sha256", "knowledge_source_sha256"):
        if meta.get(key) and not _is_sha256(meta[key]):
            reasons.append(f"运行元数据中的 {key} 不是有效 SHA-256")
    provenance = meta.get("dataset_provenance")
    if not isinstance(provenance, dict) or not provenance:
        reasons.append("最终测试集缺少独立性来源清单")
    else:
        canonical = json.dumps(
            provenance, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        expected_provenance_hash = hashlib.sha256(canonical).hexdigest()
        if meta.get("dataset_provenance_sha256") != expected_provenance_hash:
            reasons.append("测试集来源清单哈希无效或缺失")
        if provenance.get("dataset_role") != "test":
            reasons.append("来源清单的数据角色不是 test")
        if provenance.get("dataset_sha256") != meta.get("dataset_sha256"):
            reasons.append("来源清单与测试数据集哈希不一致")
        if provenance.get("derived_from_existing_eval_data") is not False:
            reasons.append("来源清单未确认测试集独立于既有评估数据")
        if provenance.get("previously_evaluated") is not False:
            reasons.append("来源清单未确认测试集此前未用于评估")
        if provenance.get("used_for_candidate_tuning") is not False:
            reasons.append("来源清单未确认测试集未用于候选调参")
        if provenance.get("curation_method") != "manual":
            reasons.append("最终测试集必须记录人工构建/标注方式")
        if not isinstance(provenance.get("curator_id"), str) or not provenance["curator_id"].strip():
            reasons.append("来源清单缺少测试集审核者标识")
        sources = provenance.get("source_references")
        if (not isinstance(sources, list) or not sources
                or any(not isinstance(source, str) or not source.strip() for source in sources)):
            reasons.append("来源清单缺少可追溯的题目来源")
    calibration = thresholds.get("calibration") or {}
    baseline_runs = calibration.get("baseline_runs")
    if (not isinstance(baseline_runs, int) or isinstance(baseline_runs, bool)
            or baseline_runs < 3):
        reasons.append("门禁阈值缺少至少 3 次重复基线的校准记录")
    if calibration.get("dataset_role") != "validation":
        reasons.append("阈值必须由 validation 数据校准")
    if not calibration.get("dataset_sha256"):
        reasons.append("阈值校准记录缺少数据集哈希")
    elif not _is_sha256(calibration["dataset_sha256"]):
        reasons.append("阈值校准数据集哈希不是有效 SHA-256")
    elif calibration.get("dataset_sha256") == meta.get("dataset_sha256"):
        reasons.append("门禁测试集不能与阈值校准集相同")
    for key in ("knowledge_version", "knowledge_source_sha256", "code_revision", "models"):
        if not calibration.get(key):
            reasons.append(f"阈值校准记录缺少 {key}")
        elif calibration.get(key) != meta.get(key):
            reasons.append(f"阈值校准与测试报告的 {key} 不一致")
    if baseline.get("status") != "complete" or candidate.get("status") != "complete":
        reasons.append("基线或候选评估未完整完成")
    if baseline.get("expected_samples") != candidate.get("expected_samples"):
        reasons.append("基线与候选预期样本数不同")
    if baseline.get("evaluated_samples") != candidate.get("evaluated_samples"):
        reasons.append("基线与候选实际评估样本数不同")
    min_samples = thresholds.get("min_samples")
    if not isinstance(min_samples, int) or isinstance(min_samples, bool) or min_samples < 1:
        reasons.append("门槛未设置有效 min_samples")
    elif (candidate.get("evaluated_samples") or 0) < min_samples:
        reasons.append(f"有效样本不足：{candidate.get('evaluated_samples', 0)} < {min_samples}")
    if meta.get("dataset_size") is not None and meta["dataset_size"] != candidate.get("expected_samples"):
        reasons.append("数据集元数据样本数与候选预期样本数不一致")

    metric_thresholds = thresholds.get("metrics") or {}
    if not metric_thresholds:
        reasons.append("尚未基于基线波动配置指标退化阈值")
    missing_thresholds = sorted(REQUIRED_METRICS - metric_thresholds.keys())
    if missing_thresholds:
        reasons.append("缺少必要门禁指标阈值：" + ", ".join(missing_thresholds))
    checks = []
    for metric, rule in metric_thresholds.items():
        if metric == "confidence_gate_refusal_rate" and "min_value" not in rule:
            reasons.append("confidence_gate_refusal_rate 缺少运营方定义的绝对拒答下限")
        if metric == "confidence_gate_false_refusal_rate" and "max_value" not in rule:
            reasons.append("confidence_gate_false_refusal_rate 缺少运营方定义的绝对误拒上限")
        before = (baseline.get("metrics") or {}).get(metric)
        after = (candidate.get("metrics") or {}).get(metric)
        check, error = _check_metric(metric, before, after, rule)
        if error:
            reasons.append(error)
        elif check:
            checks.append(check)

    if thresholds.get("require_answer_quality") is True:
        quality_thresholds = (thresholds.get("answer_quality") or {}).get("metrics") or {}
        missing_quality = sorted(REQUIRED_ANSWER_QUALITY_METRICS - quality_thresholds.keys())
        if missing_quality:
            reasons.append("缺少必要答案质量门槛：" + ", ".join(missing_quality))
        if not isinstance(answer_report, dict):
            reasons.append("门槛要求答案质量评估，但未提供答复评估报告")
        else:
            answer_meta = answer_report.get("meta") or {}
            for key in ("dataset_role", "dataset_sha256", "dataset_provenance_sha256",
                        "knowledge_version", "knowledge_source_sha256", "code_revision", "models"):
                if answer_meta.get(key) != meta.get(key):
                    reasons.append(f"检索与答复评估版本不一致：{key}")
            answer_configs = answer_meta.get("candidate_configs") or {}
            if answer_configs.get(candidate_name) != candidate.get("config"):
                reasons.append(f"检索与答复评估候选配置不一致：{candidate_name}")
            quality_values, quality_reasons = _answer_quality_values(answer_report, candidate_name)
            reasons.extend(quality_reasons)
            for metric in sorted(REQUIRED_ANSWER_QUALITY_METRICS):
                rule = quality_thresholds.get(metric) or {}
                if metric in {"answer_coverage", "faithfulness", "handling_compliance"} \
                        and "min_value" not in rule:
                    reasons.append(f"{metric} 缺少运营方定义的最低质量线")
                if metric == "unsupported_claim_rate" and "max_value" not in rule:
                    reasons.append("unsupported_claim_rate 缺少运营方定义的最高比例")
                value = quality_values.get(metric)
                check, error = _check_metric(metric, value, value, rule)
                if error:
                    reasons.append(error)
                elif check:
                    checks.append(check)

    configured_buckets = thresholds.get("required_buckets") or []
    required_buckets = set(configured_buckets)
    missing_required_buckets = REQUIRED_BUCKETS - required_buckets
    if missing_required_buckets:
        reasons.append("缺少必要评估分桶：" + ", ".join(sorted(missing_required_buckets)))
    if len(required_buckets) != len(configured_buckets):
        reasons.append("required_buckets 包含重复分桶")
    bucket_metrics = thresholds.get("bucket_metrics") or {}
    baseline_bucket_expected = 0
    candidate_bucket_expected = 0
    baseline_bucket_evaluated = 0
    candidate_bucket_evaluated = 0
    for bucket in required_buckets:
        base_bucket = (baseline.get("buckets") or {}).get(bucket) or {}
        cand_bucket = (candidate.get("buckets") or {}).get(bucket) or {}
        if not base_bucket or not cand_bucket:
            reasons.append(f"分桶指标缺失：{bucket}")
            continue
        if base_bucket.get("evaluated") != cand_bucket.get("evaluated"):
            reasons.append(f"分桶实际样本数不一致：{bucket}")
        for label, values in (("基线", base_bucket), ("候选", cand_bucket)):
            expected = values.get("expected")
            evaluated = values.get("evaluated")
            if (not isinstance(expected, int) or isinstance(expected, bool) or expected < 1
                    or not isinstance(evaluated, int) or isinstance(evaluated, bool)
                    or evaluated < 0):
                reasons.append(f"{label}分桶样本数无效：{bucket}")
            elif expected != evaluated:
                reasons.append(f"{label}分桶样本不完整：{bucket}")
        if isinstance(base_bucket.get("expected"), int):
            baseline_bucket_expected += base_bucket["expected"]
        if isinstance(cand_bucket.get("expected"), int):
            candidate_bucket_expected += cand_bucket["expected"]
        if isinstance(base_bucket.get("evaluated"), int):
            baseline_bucket_evaluated += base_bucket["evaluated"]
        if isinstance(cand_bucket.get("evaluated"), int):
            candidate_bucket_evaluated += cand_bucket["evaluated"]
        if base_bucket.get("evaluated") != base_bucket.get("expected"):
            reasons.append(f"基线分桶样本不完整：{bucket}")
        if cand_bucket.get("evaluated") != cand_bucket.get("expected"):
            reasons.append(f"候选分桶样本不完整：{bucket}")
        rules = bucket_metrics.get(bucket) or {}
        if not rules:
            reasons.append(f"未配置分桶退化门槛：{bucket}")
            continue
        for metric, rule in rules.items():
            if bucket == "D_absent" and metric == "confidence_gate_refusal_rate" \
                    and "min_value" not in rule:
                reasons.append("D_absent 拒答率缺少运营方定义的绝对下限")
            before = base_bucket.get(metric)
            after = cand_bucket.get(metric)
            check, error = _check_metric(f"{bucket}.{metric}", before, after, rule)
            if error:
                reasons.append(error)
            elif check:
                checks.append(check)

    for label, values, expected_total, evaluated_total in (
        ("基线", baseline, baseline_bucket_expected, baseline_bucket_evaluated),
        ("候选", candidate, candidate_bucket_expected, candidate_bucket_evaluated),
    ):
        if expected_total != values.get("expected_samples"):
            reasons.append(f"{label}分桶预期样本数之和与总样本数不一致")
        if evaluated_total != values.get("evaluated_samples"):
            reasons.append(f"{label}分桶已评估样本数之和与总样本数不一致")

    failed = [check for check in checks if not check["passed"]]
    if failed:
        blocking_reasons = [reason for reason in reasons
                            if not _is_threshold_configuration_reason(reason)]
        if not blocking_reasons:
            return {
                "status": "FAIL",
                "reasons": [f"{c['metric']} 超出允许退化范围" for c in failed] + reasons,
                "checks": checks,
            }
    if reasons:
        return {"status": "INCONCLUSIVE", "reasons": reasons, "checks": checks}
    if not checks:
        return {"status": "INCONCLUSIVE", "reasons": reasons or ["没有可执行的指标门槛"],
                "checks": checks}
    return {"status": "PASS", "reasons": [], "checks": checks}
