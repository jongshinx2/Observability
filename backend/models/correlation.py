from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, Field, model_validator

from backend.models.enums import Datasource


_TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")


class EvidenceScope(str, Enum):
    DIRECT = "direct"
    TEMPORAL = "temporal"


class PivotKind(str, Enum):
    TRACE_ID = "trace_id"
    SPAN_ID = "span_id"
    SERVICE_NAME = "service_name"
    INSTANCE_ID = "instance_id"
    HTTP_STATUS_CODE = "http_status_code"
    ENVIRONMENT = "environment"


class CorrelationPivot(BaseModel):
    kind: PivotKind
    value: str = Field(min_length=1, max_length=512)
    source: Datasource
    scope: EvidenceScope
    source_query: str = Field(default="", max_length=2_000)

    @model_validator(mode="after")
    def normalize_and_validate_value(self) -> "CorrelationPivot":
        value = self.value.strip()
        if self.kind in {PivotKind.TRACE_ID, PivotKind.SPAN_ID}:
            value = value.lower()

        if self.kind == PivotKind.TRACE_ID and not _TRACE_ID_PATTERN.fullmatch(value):
            raise ValueError("trace_id must be exactly 32 hexadecimal characters")
        if self.kind == PivotKind.SPAN_ID and not _SPAN_ID_PATTERN.fullmatch(value):
            raise ValueError("span_id must be exactly 16 hexadecimal characters")

        self.value = value
        return self


class TimeWindow(BaseModel):
    start_unix_ns: int = Field(ge=0)
    end_unix_ns: int = Field(ge=0)
    source: Datasource
    scope: EvidenceScope
    source_query: str = Field(default="", max_length=2_000)

    @model_validator(mode="after")
    def validate_range(self) -> "TimeWindow":
        if self.end_unix_ns < self.start_unix_ns:
            raise ValueError("end_unix_ns must be greater than or equal to start_unix_ns")
        return self


class ContextDelta(BaseModel):
    pivots: list[CorrelationPivot] = Field(default_factory=list)
    time_windows: list[TimeWindow] = Field(default_factory=list)


class InvestigationContext(BaseModel):
    pivots: list[CorrelationPivot] = Field(default_factory=list)
    time_windows: list[TimeWindow] = Field(default_factory=list)
    visited_queries: set[str] = Field(default_factory=set)
    hop_count: int = Field(default=0, ge=0)

    def values(self, kind: PivotKind) -> list[str]:
        return [pivot.value for pivot in self.pivots if pivot.kind == kind]
