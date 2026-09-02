from typing import List, Literal
from pydantic import BaseModel, Field


class Case(BaseModel):
    thesis: str
    evidence: List[str]


class AnalystOutput(BaseModel):
    long_case: Case
    short_case: Case
    direction: Literal["long", "short", "no_trade"]
    target_price: float
    timeframe_days: int
    confidence_score: int = Field(ge=0, le=100)
    why_this_side_won: str  # must reference specific evidence from the case NOT chosen


class CriticOutput(BaseModel):
    failure_scenario: str
    conviction_penalty: int = Field(ge=0, le=100)
