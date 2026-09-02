from typing import Any

from ..correlation.extractors.tempo import (
    find_attribute_values,
    is_error_span,
    iter_spans,
    span_duration_ms,
    to_int,
)
from ..models import Datasource, PivotKind, ResultStatus, ToolResult


def build_deterministic_analysis(results: list[ToolResult]) -> str:
    """LLM 최종 응답이 없을 때 관측 근거만으로 안전한 요약을 생성합니다."""
    if not results:
        return "분석에 사용할 관측성 조회 결과가 없습니다."

    lines = [
        "통합 분석 모델이 완전한 응답을 생성하지 못해 관측 데이터 요약을 제공합니다."
    ]
    for result in results:
        if result.datasource == Datasource.TEMPO:
            lines.extend(_summarize_tempo(result))
        else:
            lines.extend(_summarize_generic(result))
    return "\n".join(lines)


def _summarize_generic(result: ToolResult) -> list[str]:
    name = result.datasource.value.capitalize()
    if result.status == ResultStatus.ERROR:
        return [f"- {name} 조회 실패: {result.error or '원인 정보 없음'}"]
    if result.status == ResultStatus.EMPTY:
        return [f"- {name} 조회 결과가 없습니다. 쿼리: {result.query}"]
    return [
        f"- {name} 조회 성공: {_collection_size(result.data)}개 결과를 확인했습니다."
    ]


def _summarize_tempo(result: ToolResult) -> list[str]:
    if result.status == ResultStatus.ERROR:
        return [
            f"- Trace ID: {result.query or '알 수 없음'}",
            f"- Tempo 조회 실패: {result.error or '원인 정보 없음'}",
        ]
    if result.status == ResultStatus.EMPTY:
        return [
            f"- Trace ID: {result.query}",
            "- Tempo 조회 결과가 없습니다. Trace ID와 보존 기간을 확인하세요.",
        ]

    spans = list(iter_spans(result.data))
    services = {str(value) for value in find_attribute_values(result.data, "service.name")}
    extracted_span_count = 0
    if result.context_delta is not None:
        services.update(
            pivot.value
            for pivot in result.context_delta.pivots
            if pivot.kind == PivotKind.SERVICE_NAME
        )
        extracted_span_count = sum(
            1
            for pivot in result.context_delta.pivots
            if pivot.kind == PivotKind.SPAN_ID
        )
    durations = [
        duration for span in spans if (duration := span_duration_ms(span)) is not None
    ]
    error_spans = [span for span in spans if is_error_span(span)]
    lines = [
        f"- Trace ID: {result.query}",
        "- Tempo 조회 상태: 성공",
        f"- Span 수: {max(len(spans), extracted_span_count)}",
    ]
    if services:
        lines.append(f"- 관련 서비스: {', '.join(sorted(services))}")
    if spans and durations:
        starts = [to_int(span.get("startTimeUnixNano")) for span in spans]
        ends = [to_int(span.get("endTimeUnixNano")) for span in spans]
        valid_starts = [value for value in starts if value is not None]
        valid_ends = [value for value in ends if value is not None]
        if valid_starts and valid_ends:
            trace_duration_ms = (max(valid_ends) - min(valid_starts)) / 1_000_000
            lines.append(f"- 전체 소요 시간: {trace_duration_ms:.2f}ms")
        slowest = max(
            (
                (duration, span.get("name") or "이름 없는 span")
                for span in spans
                if (duration := span_duration_ms(span)) is not None
            ),
            default=None,
        )
        if slowest:
            lines.append(f"- 가장 느린 Span: {slowest[1]} ({slowest[0]:.2f}ms)")
    lines.append(f"- 오류 Span 수: {len(error_spans)}")
    if error_spans:
        error_names = [str(span.get("name") or "이름 없는 span") for span in error_spans[:5]]
        lines.append(f"- 오류 Span: {', '.join(error_names)}")
    if result.truncated:
        lines.append("- 한계: Trace 결과가 잘려 있어 일부 Span은 요약에서 제외됐습니다.")
    elif not spans:
        lines.append("- 한계: Tempo 응답에서 표준 Span 구조를 찾지 못했습니다.")
    return lines


def _collection_size(value: Any) -> int:
    return len(value) if isinstance(value, (list, dict)) else int(value is not None)
