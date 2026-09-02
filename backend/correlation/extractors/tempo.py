from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from backend.models.enums import Datasource
from backend.models.correlation import (
    ContextDelta,
    CorrelationPivot,
    EvidenceScope,
    PivotKind,
    TimeWindow,
)


class TempoCorrelationExtractor:
    def extract(self, data: Any, *, source_query: str) -> ContextDelta:
        pivots: list[CorrelationPivot] = []
        spans = list(iter_spans(data))

        def add_pivot(kind: PivotKind, value: Any) -> None:
            if value is None:
                return
            normalized = str(value).strip()
            if not normalized:
                return
            try:
                pivots.append(
                    CorrelationPivot(
                        kind=kind,
                        value=normalized,
                        source=Datasource.TEMPO,
                        scope=EvidenceScope.DIRECT,
                        source_query=source_query,
                    )
                )
            except ValueError:
                return

        add_pivot(PivotKind.TRACE_ID, source_query)
        for value in find_attribute_values(data, "service.name"):
            add_pivot(PivotKind.SERVICE_NAME, value)
        for value in find_attribute_values(data, "service.instance.id"):
            add_pivot(PivotKind.INSTANCE_ID, value)
        for key in ("deployment.environment.name", "deployment.environment"):
            for value in find_attribute_values(data, key):
                add_pivot(PivotKind.ENVIRONMENT, value)

        starts: list[int] = []
        ends: list[int] = []
        for span in spans:
            add_pivot(PivotKind.TRACE_ID, span.get("traceId") or span.get("trace_id"))
            add_pivot(PivotKind.SPAN_ID, span.get("spanId") or span.get("span_id"))
            add_pivot(PivotKind.SERVICE_NAME, attribute_value(span, "service.name"))
            add_pivot(PivotKind.INSTANCE_ID, attribute_value(span, "service.instance.id"))
            add_pivot(
                PivotKind.ENVIRONMENT,
                attribute_value(span, "deployment.environment.name")
                or attribute_value(span, "deployment.environment"),
            )
            add_pivot(
                PivotKind.HTTP_STATUS_CODE,
                attribute_value(span, "http.response.status_code")
                or attribute_value(span, "http.status_code"),
            )

            start = to_int(span.get("startTimeUnixNano") or span.get("start_time_unix_nano"))
            end = to_int(span.get("endTimeUnixNano") or span.get("end_time_unix_nano"))
            if start is not None:
                starts.append(start)
            if end is not None:
                ends.append(end)

        time_windows: list[TimeWindow] = []
        if starts:
            start = min(starts)
            end = max(ends) if ends else max(starts)
            time_windows.append(
                TimeWindow(
                    start_unix_ns=start,
                    end_unix_ns=max(end, start),
                    source=Datasource.TEMPO,
                    scope=EvidenceScope.DIRECT,
                    source_query=source_query,
                )
            )

        return ContextDelta(pivots=_deduplicate(pivots), time_windows=time_windows)


def iter_spans(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        if (
            "spanId" in value
            or "span_id" in value
            or "startTimeUnixNano" in value
            or "start_time_unix_nano" in value
        ):
            yield value
        for child in value.values():
            yield from iter_spans(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_spans(child)


def attribute_value(span: dict[str, Any], key: str) -> Any:
    direct = span.get(key)
    if direct is not None:
        return direct

    for container_name in ("attributes", "resourceAttributes", "resource_attributes"):
        found = _search_attributes(span.get(container_name), key)
        if found is not None:
            return found

    resource = span.get("resource")
    if isinstance(resource, dict):
        found = _search_attributes(resource.get("attributes"), key)
        if found is not None:
            return found
    return None


def find_attribute_values(value: Any, key: str) -> set[Any]:
    values: set[Any] = set()
    if isinstance(value, dict):
        if value.get("key") == key:
            found = _unwrap_value(value.get("value"))
            if isinstance(found, (str, int, float, bool)):
                values.add(found)
        if key in value:
            found = _unwrap_value(value[key])
            if isinstance(found, (str, int, float, bool)):
                values.add(found)
        for child in value.values():
            values.update(find_attribute_values(child, key))
    elif isinstance(value, list):
        for child in value:
            values.update(find_attribute_values(child, key))
    return values


def is_error_span(span: dict[str, Any]) -> bool:
    status = span.get("status")
    if isinstance(status, dict):
        code = str(status.get("code", "")).upper()
        if code in {"2", "STATUS_CODE_ERROR", "ERROR"}:
            return True
    status_code = attribute_value(span, "http.response.status_code") or attribute_value(
        span,
        "http.status_code",
    )
    parsed = to_int(status_code)
    if parsed is not None and parsed >= 500:
        return True
    for key in ("error", "error.type", "exception.type"):
        value = attribute_value(span, key)
        if value not in {None, False, "false", ""}:
            return True
    return False


def span_duration_ms(span: dict[str, Any]) -> float | None:
    start = to_int(span.get("startTimeUnixNano") or span.get("start_time_unix_nano"))
    end = to_int(span.get("endTimeUnixNano") or span.get("end_time_unix_nano"))
    if start is None or end is None or end < start:
        return None
    return (end - start) / 1_000_000


def to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _search_attributes(attributes: Any, key: str) -> Any:
    if isinstance(attributes, dict):
        if key in attributes:
            return _unwrap_value(attributes[key])
        if attributes.get("key") == key:
            return _unwrap_value(attributes.get("value"))
        for child in attributes.values():
            found = _search_attributes(child, key)
            if found is not None:
                return found
    elif isinstance(attributes, list):
        for item in attributes:
            found = _search_attributes(item, key)
            if found is not None:
                return found
    return None


def _unwrap_value(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    for key in (
        "stringValue",
        "intValue",
        "doubleValue",
        "boolValue",
        "string_value",
        "int_value",
        "double_value",
        "bool_value",
        "value",
    ):
        if key in value:
            return _unwrap_value(value[key])
    return value


def _deduplicate(pivots: list[CorrelationPivot]) -> list[CorrelationPivot]:
    unique: list[CorrelationPivot] = []
    seen: set[tuple[PivotKind, str]] = set()
    for pivot in pivots:
        key = (pivot.kind, pivot.value)
        if key in seen:
            continue
        seen.add(key)
        unique.append(pivot)
    return unique
