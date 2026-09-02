from typing import Any

from pydantic import BaseModel, Field, field_validator

from backend.models.correlation import ContextDelta, InvestigationContext
from backend.models.enums import Datasource, ResultStatus


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    user_context: str | None = ""


class ChatResponse(BaseModel):
    query: str
    reply: str
    iterations: int


class InvestigationStep(BaseModel):
    datasource: Datasource
    intent: str = Field(min_length=1, max_length=500)
    service: str | None = Field(default=None, max_length=200)
    time_range_minutes: int = Field(default=60, ge=1, le=1440)
    filters: dict[str, str] = Field(default_factory=dict)
    trace_id: str | None = None

    @field_validator("trace_id")
    @classmethod
    def normalize_trace_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if len(value) != 32 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("trace_id는 32자리 hexadecimal 문자열이어야 합니다.")
        return value


class InvestigationPlan(BaseModel):
    steps: list[InvestigationStep] = Field(min_length=1, max_length=3)
    reason: str = Field(min_length=1, max_length=1000)


class GeneratedQuery(BaseModel):
    datasource: Datasource
    query: str = Field(min_length=1, max_length=2000)
    purpose: str = Field(min_length=1, max_length=500)
    time_range_minutes: int = Field(default=60, ge=1, le=1440)
    limit: int = Field(default=20, ge=1, le=1000)


class ToolResult(BaseModel):
    datasource: Datasource
    query: str
    status: ResultStatus
    data: Any = None
    error: str | None = None
    truncated: bool = False
    context_delta: ContextDelta | None = None


class WorkflowOutcome(BaseModel):
    reply: str
    iterations: int
    plan: InvestigationPlan
    results: list[ToolResult]
    context: InvestigationContext = Field(default_factory=InvestigationContext)
