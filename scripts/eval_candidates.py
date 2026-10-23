"""Run bounded retrieval candidates on the same labeled RAG dataset.

Example: uv run python scripts/eval_candidates.py --dataset-role validation
The Milvus index and model endpoints must be available; this script never mutates
the production configuration or knowledge base.
"""
import argparse
import asyncio
import hashlib
import json
import pathlib
import subprocess
from datetime import datetime, timezone

from app.config import settings
from app.core.confidence import compute_evidence_confidence
from app.core import optimization_candidates, retrieval
from scripts.eval_ch04 import (
    GRADED_BUCKETS, RECALL_K, _mean, _ranks_per_group, _recall_at, _rr,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "data/ch11/reports/eval_candidates.json"
BUCKETS = GRADED_BUCKETS + ["D_absent"]


def _dataset(path: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _code_revision() -> str | None:
    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL,
        ).strip()
        paths = subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
            text=True, stderr=subprocess.DEVNULL,
        ).splitlines()
        digest = hashlib.sha256()
        source_exts = {".py", ".sql", ".toml", ".yaml", ".yml"}
        for name in sorted(paths):
            path = ROOT / name
            if path.is_file() and path.suffix.lower() in source_exts:
                digest.update(name.encode("utf-8"))
                digest.update(path.read_bytes())
        return f"{head}+source:{digest.hexdigest()[:12]}"
    except Exception:
        return None


def _kb_source_hash() -> str:
    digest = hashlib.sha256()
    for path in sorted((ROOT / "data/kb").glob("*.md")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


async def _score_candidate(samples: list[dict], candidate) -> dict:
    semaphore = asyncio.Semaphore(5)

    def is_transient(exc: Exception) -> bool:
        response = getattr(exc, "response", None)
        status = getattr(exc, "status_code", None) or getattr(response, "status_code", None)
        name = type(exc).__name__.lower()
        return (status == 429 or (isinstance(status, int) and 500 <= status <= 599)
                or any(token in name for token in ("timeout", "connecterror", "networkerror")))

    async def one(sample):
        async with semaphore:
            for attempt in range(4):
                try:
                    hits = await retrieval.search_knowledge(
                        sample["query"], strategy=candidate.strategy,
                        top_k=candidate.rerank_top_k, recall_top_k=candidate.recall_top_k,
                        split=candidate.split,
                    )
                    return sample, hits, None
                except Exception as exc:
                    if attempt < 3 and is_transient(exc):
                        await asyncio.sleep(0.5 * (2 ** attempt))
                        continue
                    # Keep terminal failures explicit so the release gate cannot pass partial data.
                    return sample, [], f"{type(exc).__name__}: {exc}"

    relevant = [s for s in samples if s.get("bucket") in BUCKETS]
    graded = [s for s in relevant if s.get("bucket") in GRADED_BUCKETS]
    rows = await asyncio.gather(*(one(sample) for sample in relevant))
    buckets: dict[str, dict] = {}
    failures = []
    sample_results = []
    for sample, hits, error in rows:
        annotation = {
            "expected_refusal": bool(sample.get("should_refuse", False)),
            "expected_handling": sample.get("expected_handling"),
        }
        if error:
            failures.append({"id": sample.get("id"), "error": error})
            sample_results.append({"id": sample.get("id"), "bucket": sample.get("bucket"),
                                  "status": "failed", "error": error, "metrics": None,
                                  **annotation})
            continue
        refused = (compute_evidence_confidence(hits).score
                   < settings.evidence_confidence_threshold)
        metrics = {"confidence_gate_refusal": refused}
        if sample.get("bucket") in GRADED_BUCKETS:
            ranks = _ranks_per_group(hits, sample)
            metrics.update({"recall_at_5": _recall_at(ranks, RECALL_K),
                            "mrr": _rr(ranks)})
        sample_results.append({"id": sample.get("id"), "bucket": sample.get("bucket"),
                              "status": "complete", "metrics": metrics, **annotation})
    for bucket in GRADED_BUCKETS:
        group = [(sample, hits) for sample, hits, error in rows
                 if sample.get("bucket") == bucket and error is None]
        recs, mrrs = [], []
        for sample, hits in group:
            ranks = _ranks_per_group(hits, sample)
            recs.append(_recall_at(ranks, RECALL_K))
            mrrs.append(_rr(ranks))
        expected = sum(1 for sample in graded if sample.get("bucket") == bucket)
        buckets[bucket] = {
            "expected": expected, "evaluated": len(group),
            "recall_at_5": round(_mean(recs), 4) if group else None,
            "mrr": round(_mean(mrrs), 4) if group else None,
        }
    absent_rows = [(sample, hits) for sample, hits, error in rows
                   if sample.get("bucket") == "D_absent" and error is None]
    absent_expected = sum(1 for sample in relevant if sample.get("bucket") == "D_absent")
    absent_refused = sum(
        compute_evidence_confidence(hits).score < settings.evidence_confidence_threshold
        for _, hits in absent_rows
    )
    buckets["D_absent"] = {
        "expected": absent_expected, "evaluated": len(absent_rows),
        "confidence_gate_refusal_rate": round(absent_refused / len(absent_rows), 4)
        if absent_rows else None,
    }
    recs, mrrs = [], []
    for sample, hits, error in rows:
        if error is None and sample.get("bucket") in GRADED_BUCKETS:
            ranks = _ranks_per_group(hits, sample)
            recs.append(_recall_at(ranks, RECALL_K))
            mrrs.append(_rr(ranks))
    graded_confidence = [
        compute_evidence_confidence(hits).score < settings.evidence_confidence_threshold
        for sample, hits, error in rows if sample.get("bucket") in GRADED_BUCKETS and error is None
    ]
    return {
        "status": "complete" if not failures and len(rows) == len(relevant) else "incomplete",
        "expected_samples": len(relevant), "evaluated_samples": len(rows) - len(failures),
        "metrics": {"recall_at_5": round(_mean(recs), 4) if recs else None,
                    "mrr": round(_mean(mrrs), 4) if mrrs else None,
                    "confidence_gate_refusal_rate": round(absent_refused / len(absent_rows), 4)
                    if absent_rows else None,
                    "confidence_gate_false_refusal_rate": round(
                        sum(graded_confidence) / len(graded_confidence), 4)
                    if graded_confidence else None},
        "buckets": buckets, "failures": failures, "sample_results": sample_results,
    }


def sample_differences(baseline: dict, candidate: dict) -> list[dict]:
    """Return per-question metric changes while retaining failures/missing samples."""
    before = {row.get("id"): row for row in baseline.get("sample_results", [])}
    after = {row.get("id"): row for row in candidate.get("sample_results", [])}
    changes = []
    for sample_id in sorted(before.keys() | after.keys(), key=lambda x: str(x)):
        old, new = before.get(sample_id), after.get(sample_id)
        old_metrics = old.get("metrics") if old else None
        new_metrics = new.get("metrics") if new else None
        if old_metrics != new_metrics or (old or {}).get("status") != (new or {}).get("status"):
            changes.append({
                "id": sample_id,
                "bucket": (new or old or {}).get("bucket"),
                "baseline": old_metrics,
                "candidate": new_metrics,
                "baseline_status": old.get("status") if old else "missing",
                "candidate_status": new.get("status") if new else "missing",
            })
    return changes


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=pathlib.Path,
                        default=ROOT / "tests/data/eval_ch04.jsonl")
    parser.add_argument("--dataset-role", choices=("validation", "test"), default="validation")
    parser.add_argument("--dataset-provenance", type=pathlib.Path,
                        help="independent test-set provenance JSON; required for a final PASS")
    parser.add_argument("--knowledge-version", required=True,
                        help="operator-supplied ID of the Milvus knowledge snapshot used")
    parser.add_argument("--out", type=pathlib.Path, default=OUT)
    parser.add_argument("--candidate", action="append", dest="candidates",
                        help="candidate name; repeat to select. Default evaluates all safe defaults.")
    args = parser.parse_args()
    if not args.dataset.is_absolute():
        args.dataset = ROOT / args.dataset
    samples = _dataset(args.dataset)
    provenance = None
    provenance_sha256 = None
    if args.dataset_provenance:
        provenance_path = args.dataset_provenance
        if not provenance_path.is_absolute():
            provenance_path = ROOT / provenance_path
        provenance_bytes = provenance_path.read_bytes()
        provenance = json.loads(provenance_bytes.decode("utf-8"))
        provenance_canonical = json.dumps(
            provenance, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        provenance_sha256 = hashlib.sha256(provenance_canonical).hexdigest()
    available = {c.name: c for c in optimization_candidates.default_candidates()}
    selected = args.candidates or list(available)
    missing = sorted(set(selected) - available.keys())
    if missing:
        parser.error(f"unknown candidates: {', '.join(missing)}")

    raw = args.dataset.read_bytes()
    report = {
        "schema_version": 1,
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "dataset": str(args.dataset.relative_to(ROOT)) if args.dataset.is_relative_to(ROOT) else str(args.dataset),
            "dataset_role": args.dataset_role,
            "dataset_sha256": hashlib.sha256(raw).hexdigest(),
            "dataset_size": len(samples), "code_revision": _code_revision(),
            "dataset_provenance": provenance,
            "dataset_provenance_sha256": provenance_sha256,
            "knowledge_version": args.knowledge_version,
            "knowledge_source_sha256": _kb_source_hash(),
            "models": {"embed": settings.embed_model, "rerank": settings.rerank_model,
                       "chat": settings.chat_model},
            "metric_definition": (
                "section-group recall@5 and reciprocal rank; cross-document groups are averaged. "
                "confidence_gate_refusal_rate measures only the retrieval confidence threshold, "
                "not semantic self-check or final answer correctness; expected_handling is an "
                "annotation for human review and is not automatically scored."
            ),
        },
        "candidates": {},
    }
    for name in selected:
        candidate = available[name]
        print(f"Evaluating {name}: {candidate.to_dict()}", flush=True)
        result = await _score_candidate(samples, candidate)
        report["candidates"][name] = {"config": candidate.to_dict(), **result}
        print(f"  {result['status']} {result['metrics']}", flush=True)
    baseline = report["candidates"].get("baseline")
    if baseline:
        report["comparisons"] = {
            name: {"changed_sample_count": len(changes), "changed_samples": changes}
            for name, result in report["candidates"].items()
            for changes in [sample_differences(baseline, result)]
        }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Report: {args.out}")
    return 0 if all(v["status"] == "complete" for v in report["candidates"].values()) else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
