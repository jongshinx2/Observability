from __future__ import annotations

from typing import Any, Protocol

from backend.models.correlation import ContextDelta


class CorrelationExtractor(Protocol):
    def extract(self, data: Any, *, source_query: str) -> ContextDelta:
        ...
