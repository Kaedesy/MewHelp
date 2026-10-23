"""Create bounded offline-evaluation proposals from human-confirmed cases.

This module never changes runtime settings. It only maps confirmed failure
categories to already validated retrieval profiles for offline evaluation.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from app.core.optimization_candidates import default_candidates

MIN_CASES_PER_PROPOSAL = 3

NEXT_STEPS = {
    "retrieval_miss": "evaluate alternate recall depth on regression and challenge sets",
    "rerank_error": "evaluate alternate rerank depth on regression and challenge sets",
    "evidence_insufficient": "review answer handling and evidence threshold; no retrieval auto-change",
    "generation_error": "inspect generation traces and run answer-quality evaluation",
    "tool_error": "inspect tool audit logs and tool-specific reliability; no retrieval auto-change",
    "routing_error": "run intent/routing evaluation; no retrieval auto-change",
    "knowledge_gap": "continue through the existing deduplicated knowledge review queue",
    "unknown": "collect more evidence and human diagnosis before proposing a change",
}


def _value(case, name: str):
    return case.get(name) if isinstance(case, dict) else getattr(case, name, None)


def propose_from_confirmed_cases(cases: list, *, min_cases: int = MIN_CASES_PER_PROPOSAL) -> dict:
    """Return reviewable offline candidates from cases with human-confirmed labels."""
    if min_cases < 1:
        raise ValueError("min_cases must be at least 1")

    grouped: dict[str, list[str]] = defaultdict(list)
    ignored = 0
    for case in cases:
        if _value(case, "diagnosis_status") != "confirmed":
            ignored += 1
            continue
        failure_type = _value(case, "human_failure_type") or _value(case, "failure_type")
        case_id = _value(case, "case_id")
        if failure_type not in NEXT_STEPS or not case_id:
            ignored += 1
            continue
        grouped[failure_type].append(str(case_id))

    profiles = default_candidates()
    baseline = next(item for item in profiles if item.name == "baseline")
    proposals = []
    for failure_type, case_ids in sorted(grouped.items()):
        candidates = []
        if len(set(case_ids)) >= min_cases and failure_type in {"retrieval_miss", "rerank_error"}:
            for candidate in profiles:
                if candidate.name == "baseline":
                    continue
                changed = (candidate.recall_top_k != baseline.recall_top_k
                           if failure_type == "retrieval_miss"
                           else candidate.rerank_top_k != baseline.rerank_top_k)
                if changed:
                    candidates.append({
                        "name": candidate.name,
                        "config": candidate.to_dict(),
                        "supporting_case_ids": sorted(set(case_ids)),
                        "requires_offline_evaluation": True,
                        "runtime_change": False,
                    })
        proposals.append({
            "failure_type": failure_type,
            "confirmed_case_count": len(set(case_ids)),
            "minimum_cases": min_cases,
            "next_step": NEXT_STEPS[failure_type],
            "candidate_profiles": candidates,
            "status": ("candidate_proposed" if candidates else
                       "collect_more_cases" if len(set(case_ids)) < min_cases else
                       "no_bounded_retrieval_candidate"),
        })

    counts = Counter({kind: len(set(ids)) for kind, ids in grouped.items()})
    return {
        "proposal_version": "confirmed-failures-v1",
        "confirmed_cases_scanned": sum(counts.values()),
        "ignored_unconfirmed_or_invalid_cases": ignored,
        "minimum_cases_per_proposal": min_cases,
        "failure_counts": dict(sorted(counts.items())),
        "proposals": proposals,
        "runtime_settings_changed": False,
        "decision_policy": "offline evaluation and release gate are required before operator review",
    }
