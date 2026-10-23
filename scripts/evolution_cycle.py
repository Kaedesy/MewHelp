"""Safely select retrieval candidates from failure-focused evals.

This command compares candidate reports on two development sets and checks each
candidate against the independent-test release gate. It only emits a
recommendation; it never changes runtime settings or the knowledge base.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import pathlib
import re
import sys
from datetime import datetime, timezone

from app.core.release_gate import evaluate_gate

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _read(path: pathlib.Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()


def _complete(report: dict, label: str) -> list[str]:
    errors = []
    meta = report.get("meta") or {}
    if meta.get("dataset_role") != "validation":
        errors.append(f"{label} must have dataset_role=validation")
    if not meta.get("dataset_sha256") or not meta.get("code_revision"):
        errors.append(f"{label} is missing dataset hash or code revision")
    candidates = report.get("candidates") or {}
    if "baseline" not in candidates:
        errors.append(f"{label} has no baseline candidate")
    for name, candidate in candidates.items():
        if candidate.get("status") != "complete":
            errors.append(f"{label}.{name} is not complete")
        if candidate.get("evaluated_samples") != candidate.get("expected_samples"):
            errors.append(f"{label}.{name} has incomplete samples")
    return errors


def _sample_count(report: dict) -> int:
    return int((report.get("meta") or {}).get("dataset_size") or 0)


def _positive_gain(report: dict, name: str) -> dict:
    candidates = report.get("candidates") or {}
    baseline = candidates.get("baseline") or {}
    candidate = candidates.get(name) or {}
    before = baseline.get("metrics") or {}
    after = candidate.get("metrics") or {}
    return {
        "recall_at_5_delta": round(after.get("recall_at_5", 0) - before.get("recall_at_5", 0), 4),
        "mrr_delta": round(after.get("mrr", 0) - before.get("mrr", 0), 4),
        "candidate_config": candidate.get("config"),
    }


def _failure_cases(report: dict) -> dict:
    """Extract baseline retrieval misses and the buckets they belong to."""
    rows = (report.get("candidates", {}).get("baseline", {}).get("sample_results") or [])
    misses = [row for row in rows
              if row.get("status") == "complete"
              and isinstance((row.get("metrics") or {}).get("recall_at_5"), (int, float))
              and row["metrics"]["recall_at_5"] < 1.0]
    buckets: dict[str, list[str]] = {}
    for row in misses:
        buckets.setdefault(row.get("bucket", "unknown"), []).append(str(row.get("id")))
    return {"count": len(misses), "case_ids": [str(row.get("id")) for row in misses],
            "by_bucket": buckets}


def _case_changes(report: dict, name: str) -> dict:
    candidates = report.get("candidates") or {}
    baseline_rows = {row.get("id"): row for row in
                     (candidates.get("baseline", {}).get("sample_results") or [])}
    candidate_rows = {row.get("id"): row for row in
                      (candidates.get(name, {}).get("sample_results") or [])}
    fixed, regressed = [], []
    for case_id, before in baseline_rows.items():
        after = candidate_rows.get(case_id)
        old = (before.get("metrics") or {}).get("recall_at_5")
        new = ((after or {}).get("metrics") or {}).get("recall_at_5")
        if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
            continue
        if new > old:
            fixed.append(str(case_id))
        elif new < old:
            regressed.append(str(case_id))
    return {"improved_case_ids": fixed, "regressed_case_ids": regressed}


def _review_summary(path: pathlib.Path, regression: dict, challenge: dict) -> dict:
    expected: dict[str, dict[str, set[str]]] = {}
    for dataset, report in (("regression", regression), ("challenge", challenge)):
        for row in ((report.get("candidates", {}).get("baseline") or {}).get("sample_results") or []):
            case_id = str(row.get("id") or "")
            family = str(row.get("parent_case_id") or "").strip()
            if not family:
                match = re.match(r"^(?:FR|FC)-(E\d+)-", case_id)
                family = match.group(1) if match else ""
            if not family:
                continue
            expected.setdefault(family, {"regression": set(), "challenge": set()})
            expected[family][dataset].add(case_id)
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        rows = list(csv.DictReader(source))
    seen = [row.get("failure_family", "").strip() for row in rows]
    approved = {
        row.get("failure_family", "").strip()
        for row in rows
        if row.get("review_status", "").strip().lower() == "approved"
        and row.get("reviewer_id", "").strip()
    }
    duplicates = len(seen) != len(set(seen))
    missing = sorted(set(expected) - set(seen))
    unexpected = sorted(set(seen) - set(expected))
    malformed = []
    for row in rows:
        family = row.get("failure_family", "").strip()
        if family not in expected:
            continue
        for dataset in ("regression", "challenge"):
            recorded = {value.strip() for value in row.get(f"{dataset}_case_ids", "").split(";") if value.strip()}
            if recorded != expected[family][dataset]:
                malformed.append(family)
                break
    complete = (not duplicates and not missing and not unexpected and not malformed
                and approved == set(expected))
    return {
        "complete": complete,
        "review_granularity": "failure_family",
        "expected_categories": len(expected),
        "reviewed_approved_categories": len(approved),
        "pending_or_unapproved_categories": len(set(expected) - approved),
        "duplicate_rows": duplicates,
        "missing_categories": missing,
        "unexpected_categories": unexpected,
        "case_id_mismatch_categories": sorted(set(malformed)),
        "reviewer_ids": sorted({row.get("reviewer_id", "").strip() for row in rows
                                 if row.get("review_status", "").strip().lower() == "approved"
                                 and row.get("reviewer_id", "").strip()}),
    }


def build_cycle(regression: dict, challenge: dict, independent: dict,
                answer_report: dict, thresholds: dict,
                regression_manifest: dict, challenge_manifest: dict,
                development_review: dict, hashes: dict) -> dict:
    errors = _complete(regression, "regression") + _complete(challenge, "challenge")
    rmeta, cmeta = regression.get("meta") or {}, challenge.get("meta") or {}
    imeta = independent.get("meta") or {}
    if imeta.get("dataset_role") != "test":
        errors.append("independent report must have dataset_role=test")
    for field in ("code_revision", "knowledge_version", "knowledge_source_sha256"):
        if rmeta.get(field) != cmeta.get(field):
            errors.append(f"regression and challenge differ in {field}")
    if rmeta.get("dataset_sha256") == cmeta.get("dataset_sha256"):
        errors.append("regression and challenge must be different datasets")

    r_candidates = regression.get("candidates") or {}
    c_candidates = challenge.get("candidates") or {}
    names = (set(r_candidates) & set(c_candidates)) - {"baseline"}
    if not names:
        errors.append("no shared non-baseline candidates")
    for name in sorted(names):
        if r_candidates[name].get("config") != c_candidates[name].get("config"):
            errors.append(f"candidate config differs between development reports: {name}")

    items = {}
    proposals = {}
    for name in sorted(names):
        reg_gain = _positive_gain(regression, name)
        challenge_gain = _positive_gain(challenge, name)
        reg_changes = _case_changes(regression, name)
        challenge_changes = _case_changes(challenge, name)
        gate = evaluate_gate(independent, name, thresholds, answer_report=answer_report)
        both_sets_improve = (reg_gain["recall_at_5_delta"] > 0
                             and challenge_gain["recall_at_5_delta"] > 0
                             and reg_gain["mrr_delta"] >= 0
                             and challenge_gain["mrr_delta"] >= 0)
        datasets_approved = development_review.get("complete") is True
        if not both_sets_improve:
            disposition = "reject_no_consistent_targeted_gain"
        elif gate.get("status") != "PASS":
            disposition = f"reject_release_gate_{gate.get('status', 'UNKNOWN').lower()}"
        elif not datasets_approved:
            disposition = "pending_human_review_of_development_sets"
        else:
            disposition = "eligible_for_operator_review"
        items[name] = {
            "development_regression": reg_gain,
            "development_challenge": challenge_gain,
            "improved_cases": {
                "regression": reg_changes["improved_case_ids"],
                "challenge": challenge_changes["improved_case_ids"],
            },
            "regressed_cases": {
                "regression": reg_changes["regressed_case_ids"],
                "challenge": challenge_changes["regressed_case_ids"],
            },
            "targeted_gain_on_both_sets": both_sets_improve,
            "development_sets_human_approved": datasets_approved,
            "release_gate": gate,
            "disposition": disposition,
        }
        base_config = (r_candidates.get("baseline") or {}).get("config") or {}
        candidate_config = r_candidates[name].get("config") or {}
        changed_params = {key: {"baseline": base_config.get(key), "candidate": value}
                          for key, value in candidate_config.items()
                          if base_config.get(key) != value}
        proposals[name] = {
            "bounded_candidate_config": candidate_config,
            "changed_parameters": changed_params,
            "triggering_failures": {
                "regression": _failure_cases(regression),
                "challenge": _failure_cases(challenge),
            },
            "hypothesis": ("The changed retrieval parameter should improve the identified "
                           "baseline miss cases; evaluation and release gate decide whether to keep it."),
        }

    eligible = [name for name, item in items.items()
                if item["disposition"] == "eligible_for_operator_review"]
    # Test fixtures derived from earlier evaluation data or provisional thresholds
    # can provide evidence, but cannot authorize an automatic production change.
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cycle_type": "failure_driven_retrieval_candidate",
        "status": "INCONCLUSIVE" if errors else ("RECOMMENDATION_READY" if eligible else "NO_CANDIDATE"),
        "runtime_settings_changed": False,
        "errors": errors,
        "dataset_roles": {
            "regression": rmeta.get("dataset_role"),
            "challenge": cmeta.get("dataset_role"),
            "independent_test": ((independent.get("meta") or {}).get("dataset_role")),
        },
        "dataset_sizes": {"regression": _sample_count(regression),
                          "challenge": _sample_count(challenge)},
        "code_revision": rmeta.get("code_revision"),
        "knowledge_version": rmeta.get("knowledge_version"),
        "development_curation": {
            "regression": regression_manifest.get("curation_status"),
            "challenge": challenge_manifest.get("curation_status"),
            "review": development_review,
        },
        "threshold_status": ((thresholds.get("calibration") or {})
                              .get("operator_safety_limits", {}).get("status")),
        "candidate_decisions": items,
        "candidate_proposals": proposals,
        "eligible_candidates": eligible,
        "note": ("A recommendation is not a deployment. The operator must review and apply it; "
                 "the command never updates production settings or knowledge."),
        "source_sha256": hashes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regression-report", type=pathlib.Path, required=True)
    parser.add_argument("--challenge-report", type=pathlib.Path, required=True)
    parser.add_argument("--independent-report", type=pathlib.Path, required=True,
                        help="independent retrieval report consumed by the release gate")
    parser.add_argument("--answer-report", type=pathlib.Path, required=True)
    parser.add_argument("--thresholds", type=pathlib.Path, required=True)
    parser.add_argument("--regression-manifest", type=pathlib.Path, required=True)
    parser.add_argument("--challenge-manifest", type=pathlib.Path, required=True)
    parser.add_argument("--development-review", type=pathlib.Path, required=True,
                        help="CSV review record covering each failure family in both development sets")
    parser.add_argument("--out", type=pathlib.Path, required=True)
    args = parser.parse_args()
    paths = {
        "regression": args.regression_report,
        "challenge": args.challenge_report,
        "independent": args.independent_report,
        "answers": args.answer_report,
        "thresholds": args.thresholds,
        "regression_manifest": args.regression_manifest,
        "challenge_manifest": args.challenge_manifest,
    }
    try:
        values, hashes = {}, {}
        for key, path in paths.items():
            if not path.is_absolute():
                path = ROOT / path
            values[key], hashes[str(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path)] = _read(path)
        review_path = args.development_review
        if not review_path.is_absolute():
            review_path = ROOT / review_path
        review = _review_summary(review_path, values["regression"], values["challenge"])
        hashes[str(review_path.relative_to(ROOT) if review_path.is_relative_to(ROOT) else review_path)] = hashlib.sha256(review_path.read_bytes()).hexdigest()
        for source in (ROOT / "scripts/evolution_cycle.py",
                       ROOT / "app/core/evolution_proposals.py",
                       ROOT / "app/api/diagnosis.py"):
            hashes[str(source.relative_to(ROOT))] = hashlib.sha256(source.read_bytes()).hexdigest()
        report = build_cycle(
            values["regression"], values["challenge"], values["independent"],
            values["answers"], values["thresholds"],
            values["regression_manifest"], values["challenge_manifest"], review, hashes,
        )
        out = args.out if args.out.is_absolute() else ROOT / args.out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "INCONCLUSIVE", "errors": [str(exc)]}, ensure_ascii=False))
        return 2
    print(json.dumps({"status": report["status"],
                      "eligible_candidates": report["eligible_candidates"],
                      "dispositions": {name: item["disposition"]
                                       for name, item in report["candidate_decisions"].items()},
                      "report": str(out)}, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "RECOMMENDATION_READY" else 2


if __name__ == "__main__":
    sys.exit(main())
