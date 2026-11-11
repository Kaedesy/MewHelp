# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
from typing import Literal

from pydantic import BaseModel, Field


class ApproveRequest(BaseModel):
    approved_answer: str


class PairRelationReviewRequest(BaseModel):
    decision: Literal["merge", "no_merge", "uncertain"]
    reviewer_id: str = Field(min_length=1, max_length=128)
    comment: str | None = Field(default=None, max_length=4000)
