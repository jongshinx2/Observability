import contextvars
import functools
import json
import logging
import logging.handlers
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar


_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "srelens_request_id",
    default="-",
)
_configured = False
_STANDARD_LOG_RECORD_FIELDS = set(logging.makeLogRecord({}).__dict__)
_STANDARD_LOG_RECORD_FIELDS.update({"message", "asctime"})
T = TypeVar("T")


def new_request_id() -> str:
    return uuid.uuid4().hex


def get_request_id() -> str:
    return _request_id.get()


def bind_request_id(request_id: str) -> contextvars.Token[str]:
    return _request_id.set(request_id)


def reset_request_id(token: contextvars.Token[str]) -> None:
    _request_id.reset(token)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", record.getMessage()),
            "request_id": get_request_id(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_LOG_RECORD_FIELDS and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = (
            f"{datetime.now(timezone.utc).isoformat()} "
            f"{record.levelname} request_id={get_request_id()} "
            f"event={getattr(record, 'event', record.getMessage())}"
        )
        fields = []
        for key, value in record.__dict__.items():
            if key not in _STANDARD_LOG_RECORD_FIELDS and key != "event":
                fields.append(f"{key}={value!r}")
        if fields:
            base = f"{base} {' '.join(fields)}"
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


def configure_logging(
    level: str = "INFO",
    log_format: str = "json",
    log_file: str | Path | None = None,
) -> None:
    """SRELens 로거를 콘솔과 선택적 회전 파일에 설정합니다."""
    global _configured
    if _configured:
        return

    logger = logging.getLogger("srelens")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    formatter: logging.Formatter = (
        JsonFormatter() if log_format.lower() == "json" else TextFormatter()
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"srelens.{name}")


def _result_fields(result: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    datasource = getattr(result, "datasource", None)
    status = getattr(result, "status", None)
    query = getattr(result, "query", None)
    error = getattr(result, "error", None)
    truncated = getattr(result, "truncated", None)
    data = getattr(result, "data", None)
    steps = getattr(result, "steps", None)
    if datasource is not None:
        fields["datasource"] = getattr(datasource, "value", datasource)
    if status is not None:
        fields["status"] = getattr(status, "value", status)
    if query is not None and datasource is not None:
        fields["query"] = query
    if error:
        fields["error"] = error
    if truncated is not None:
        fields["truncated"] = truncated
    if isinstance(data, (list, dict)):
        fields["result_count"] = len(data)
    if steps is not None:
        fields["step_count"] = len(steps)
    if isinstance(result, str):
        fields["output_chars"] = len(result)
    return fields


def trace_async(component: str) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """비동기 함수의 시작, 완료, 예외와 실행 시간을 공통 형식으로 기록합니다."""
    def decorator(function: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        logger = get_logger(component)
        function_name = function.__qualname__

        @functools.wraps(function)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            request_token = None
            if get_request_id() == "-":
                request_token = bind_request_id(new_request_id())
            started = time.perf_counter()
            logger.info(
                "function.started",
                extra={
                    "event": "function.started",
                    "component": component,
                    "function_name": function_name,
                },
            )
            try:
                result = await function(*args, **kwargs)
            except Exception as exc:
                logger.exception(
                    "function.failed",
                    extra={
                        "event": "function.failed",
                        "component": component,
                        "function_name": function_name,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
                if request_token is not None:
                    reset_request_id(request_token)
                raise
            logger.info(
                "function.completed",
                extra={
                    "event": "function.completed",
                    "component": component,
                    "function_name": function_name,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    **_result_fields(result),
                },
            )
            if request_token is not None:
                reset_request_id(request_token)
            return result

        return wrapper
    return decorator
