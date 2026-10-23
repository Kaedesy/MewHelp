"""Configurable deterministic audit sampling for diagnosis cases.

Sampling chooses cases for a human quality audit. It never approves knowledge
or changes the diagnosis; confirmed knowledge gaps still use the existing
clustered review queue before any write-back.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.config import settings

FAILURE_TYPES = {
    "retrieval_miss", "rerank_error", "evidence_insufficient", "generation_error",
    "tool_error", "routing_error", "knowledge_gap", "unknown",
}


def load_policy() -> dict:
    path = Path(settings.diagnosis_review_policy_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    policy = json.loads(path.read_text(encoding="utf-8"))
    if not policy.get("policy_version") or not isinstance(policy.get("categories"), dict):
        raise ValueError("diagnosis review policy must define policy_version and categories")
    for failure_type, rule in policy["categories"].items():
        rate = rule.get("sample_rate") if isinstance(rule, dict) else None
        if failure_type not in FAILURE_TYPES or not isinstance(rate, (int, float)) or not 0 <= rate <= 1:
            raise ValueError(f"invalid diagnosis review policy category: {failure_type}")
    if set(policy["categories"]) != FAILURE_TYPES:
        raise ValueError("diagnosis review policy must configure every failure type")
    risk = policy.setdefault("risk_escalation", {})
    risk.setdefault("evidence_confidence_below", settings.evidence_confidence_threshold)
    if not set(risk.get("always_review_failure_types") or []).issubset(FAILURE_TYPES):
        raise ValueError("risk escalation contains an unknown failure type")
    threshold = risk.get("evidence_confidence_below")
    if threshold is not None and (not isinstance(threshold, (int, float)) or not 0 <= threshold <= 1):
        raise ValueError("evidence_confidence_below must be between 0 and 1")
    return policy


def review_decision(case: dict, policy: dict | None = None) -> dict:
    policy = policy or load_policy()
    failure_type = case.get("failure_type")
    rule = (policy.get("categories") or {}).get(failure_type)
    if rule is None:
        return {"selected": False, "reason": "category_not_configured"}

    risk = policy.get("risk_escalation") or {}
    if failure_type in (risk.get("always_review_failure_types") or []):
        return {"selected": True, "reason": "risk_escalation", "sample_rate": rule["sample_rate"]}
    confidence = case.get("evidence_confidence")
    threshold = risk.get("evidence_confidence_below")
    if confidence is not None and threshold is not None and confidence < threshold:
        return {"selected": True, "reason": "low_evidence_confidence", "sample_rate": rule["sample_rate"]}

    rate = float(rule["sample_rate"])
    digest = hashlib.sha256(
        f"{policy['policy_version']}:{case.get('case_id', '')}".encode("utf-8")
    ).digest()
    draw = int.from_bytes(digest[:8], "big") / (2**64 - 1)
    return {"selected": draw < rate,
            "reason": "sampled" if draw < rate else "outside_sample",
            "sample_rate": rate}
