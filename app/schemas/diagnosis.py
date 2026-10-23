from typing import Literal

from pydantic import BaseModel, Field


FailureType = Literal[
    "retrieval_miss", "rerank_error", "evidence_insufficient", "generation_error",
    "tool_error", "routing_error", "knowledge_gap", "unknown",
]
DiagnosisStatus = Literal["confirmed", "false_positive", "insufficient_data"]


class DiagnosisUpdate(BaseModel):
    failure_type: FailureType
    diagnosis_status: DiagnosisStatus
    diagnosis_reason: str = Field(min_length=3, max_length=4000)
