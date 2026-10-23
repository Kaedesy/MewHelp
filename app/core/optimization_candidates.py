"""Versioned, bounded retrieval candidates used by offline evaluation only."""
from dataclasses import asdict, dataclass

from app.config import settings


@dataclass(frozen=True)
class RetrievalCandidate:
    name: str
    recall_top_k: int
    rerank_top_k: int
    strategy: str = "hybrid_rerank"
    split: bool = True

    def validate(self) -> "RetrievalCandidate":
        if not self.name or len(self.name) > 64:
            raise ValueError("candidate name must contain 1-64 characters")
        if self.strategy != "hybrid_rerank":
            raise ValueError("candidate strategy must be hybrid_rerank")
        if not 1 <= self.recall_top_k <= 200:
            raise ValueError("recall_top_k must be between 1 and 200")
        if not 1 <= self.rerank_top_k <= min(50, self.recall_top_k):
            raise ValueError("rerank_top_k must be between 1 and min(50, recall_top_k)")
        return self

    def to_dict(self) -> dict:
        return asdict(self)


def default_candidates() -> list[RetrievalCandidate]:
    """One current-settings baseline and two single-variable alternatives."""
    recall, rerank = settings.recall_top_k, settings.rerank_top_k
    out = [RetrievalCandidate("baseline", recall, rerank)]
    if recall > 20:
        out.append(RetrievalCandidate("recall_20", 20, min(rerank, 20)))
    if rerank > 5:
        out.append(RetrievalCandidate("rerank_5", recall, 5))
    else:
        out.append(RetrievalCandidate("recall_plus_20", min(200, recall + 20), rerank))
    return [candidate.validate() for candidate in out]


def parse_candidate(payload: dict) -> RetrievalCandidate:
    return RetrievalCandidate(**payload).validate()
