import json
from typing import Any


def compact_data(data: Any, max_chars: int) -> tuple[Any, bool]:
    """도구 결과를 유효한 JSON 구조로 유지하면서 크기를 제한합니다."""
    serialized = json.dumps(data, ensure_ascii=False)
    if len(serialized) <= max_chars:
        return data, False
    return {
        "preview": serialized[:max_chars],
        "warning": "도구 결과가 커서 미리보기만 포함했습니다.",
    }, True


def describe_exception(exc: Exception, response_limit: int = 1000) -> tuple[str, dict[str, Any]]:
    """HTTP 오류 응답을 포함하되 로그와 사용자 오류 문자열의 크기를 제한합니다."""
    detail = str(exc)
    fields: dict[str, Any] = {
        "error_type": type(exc).__name__,
        "error": detail,
    }
    response = getattr(exc, "response", None)
    if response is None:
        return detail, fields

    status_code = getattr(response, "status_code", None)
    try:
        response_body = response.text
    except Exception:
        response_body = ""
    response_preview = response_body[:response_limit]
    if status_code is not None:
        fields["http_status"] = status_code
    if response_preview:
        fields["response_body"] = response_preview
    suffix = f"HTTP {status_code}" if status_code is not None else "HTTP 오류"
    if response_preview:
        suffix = f"{suffix}: {response_preview}"
    return f"{detail} ({suffix})", fields
