from __future__ import annotations

from dataclasses import dataclass

from backend.models.enums import Datasource
from backend.models.correlation import (
    ContextDelta,
    EvidenceScope,
    InvestigationContext,
    PivotKind,
    TimeWindow,
)


@dataclass(frozen=True)
class ContextMergeResult:
    context: InvestigationContext
    added_pivots: int
    promoted_pivots: int
    added_time_windows: int


class InvestigationContextMerger:
    def __init__(
        self,
        *,
        max_hops: int = 4,
        buffer_seconds: int = 120,
        max_trace_ids: int = 10,
        max_services: int = 10,
        max_span_ids: int = 100,
    ) -> None:
        self.max_hops = max_hops
        self.buffer_seconds = buffer_seconds
        self.max_trace_ids = max_trace_ids
        self.max_services = max_services
        self.max_span_ids = max_span_ids

    def absorb(
        self,
        context: InvestigationContext,
        delta: ContextDelta,
    ) -> ContextMergeResult:
        merged = context.model_copy(deep=True)
        pivot_indexes = {
            (pivot.kind, pivot.value): index
            for index, pivot in enumerate(merged.pivots)
        }
        added_pivots = 0
        promoted_pivots = 0

        for pivot in delta.pivots:
            key = (pivot.kind, pivot.value)
            existing_index = pivot_indexes.get(key)
            if existing_index is not None:
                existing = merged.pivots[existing_index]
                if existing.scope == EvidenceScope.TEMPORAL and pivot.scope == EvidenceScope.DIRECT:
                    merged.pivots[existing_index] = pivot.model_copy(deep=True)
                    promoted_pivots += 1
                continue

            if self._at_pivot_limit(merged, pivot.kind):
                continue

            pivot_indexes[key] = len(merged.pivots)
            merged.pivots.append(pivot.model_copy(deep=True))
            added_pivots += 1

        existing_windows = {
            (window.start_unix_ns, window.end_unix_ns, window.source, window.scope)
            for window in merged.time_windows
        }
        added_time_windows = 0
        for window in delta.time_windows:
            key = (window.start_unix_ns, window.end_unix_ns, window.source, window.scope)
            if key in existing_windows:
                continue
            existing_windows.add(key)
            merged.time_windows.append(window.model_copy(deep=True))
            added_time_windows += 1

        return ContextMergeResult(
            context=merged,
            added_pivots=added_pivots,
            promoted_pivots=promoted_pivots,
            added_time_windows=added_time_windows,
        )

    def register_query(
        self,
        context: InvestigationContext,
        datasource: Datasource,
        query: str,
    ) -> InvestigationContext:
        updated = context.model_copy(deep=True)
        updated.visited_queries.add(f"{datasource.value}:{query.strip()}")
        return updated

    def advance_hop(self, context: InvestigationContext) -> InvestigationContext:
        if context.hop_count >= self.max_hops:
            return context
        updated = context.model_copy(deep=True)
        updated.hop_count += 1
        return updated

    def can_advance(self, context: InvestigationContext) -> bool:
        return context.hop_count < self.max_hops

    def primary_time_window(self, context: InvestigationContext) -> TimeWindow | None:
        if not context.time_windows:
            return None

        source_priority = {
            Datasource.TEMPO: 0,
            Datasource.LOKI: 1,
            Datasource.PROMETHEUS: 2,
        }
        return min(
            context.time_windows,
            key=lambda window: (
                0 if window.scope == EvidenceScope.DIRECT else 1,
                source_priority.get(window.source, 99),
                window.end_unix_ns - window.start_unix_ns,
            ),
        )

    def buffered_time_window(
        self,
        context: InvestigationContext,
        buffer_seconds: int | None = None,
    ) -> TimeWindow | None:
        primary = self.primary_time_window(context)
        if primary is None:
            return None
        buffer_ns = (self.buffer_seconds if buffer_seconds is None else buffer_seconds) * 1_000_000_000
        return primary.model_copy(
            update={
                "start_unix_ns": max(0, primary.start_unix_ns - buffer_ns),
                "end_unix_ns": primary.end_unix_ns + buffer_ns,
            }
        )

    def _at_pivot_limit(self, context: InvestigationContext, kind: PivotKind) -> bool:
        limit = {
            PivotKind.TRACE_ID: self.max_trace_ids,
            PivotKind.SERVICE_NAME: self.max_services,
            PivotKind.SPAN_ID: self.max_span_ids,
        }.get(kind)
        if limit is None:
            return False
        return sum(1 for pivot in context.pivots if pivot.kind == kind) >= limit
