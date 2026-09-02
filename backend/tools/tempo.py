import httpx
from pydantic import BaseModel, Field, field_validator

from ..correlation.extractors.tempo import TempoCorrelationExtractor
from ..models import Datasource, ResultStatus, ToolResult
from ..observability import get_logger, trace_async
from .base import compact_data, describe_exception


logger = get_logger("tempo_tool")


class TempoQuery(BaseModel):
    trace_id: str = Field(pattern=r"^[0-9a-fA-F]{32}$")

    @field_validator("trace_id")
    @classmethod
    def normalize_trace_id(cls, value: str) -> str:
        return value.lower()


class TempoTool:
    def __init__(self, url: str, timeout: float, max_output_chars: int):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.max_output_chars = max_output_chars
        self.correlation_extractor = TempoCorrelationExtractor()

    @trace_async("tempo_tool")
    async def execute(self, trace_id: str) -> ToolResult:
        try:
            validated = TempoQuery(trace_id=trace_id)
            request_url = f"{self.url}/{validated.trace_id}"
            logger.info(
                "datasource.request.started",
                extra={
                    "event": "datasource.request.started",
                    "function_name": "TempoTool.execute",
                    "datasource": Datasource.TEMPO.value,
                    "url": request_url,
                    "query": validated.trace_id,
                },
            )
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(request_url)
            response.raise_for_status()
            trace_data = response.json()
            logger.info(
                "datasource.response.received",
                extra={
                    "event": "datasource.response.received",
                    "function_name": "TempoTool.execute",
                    "datasource": Datasource.TEMPO.value,
                    "http_status": response.status_code,
                    "has_data": bool(trace_data),
                },
            )
            if not trace_data:
                return ToolResult(
                    datasource=Datasource.TEMPO,
                    query=validated.trace_id,
                    status=ResultStatus.EMPTY,
                    data=None,
                )
            context_delta = self.correlation_extractor.extract(
                trace_data,
                source_query=validated.trace_id,
            )
            logger.info(
                "correlation.delta.extracted",
                extra={
                    "event": "correlation.delta.extracted",
                    "function_name": "TempoTool.execute",
                    "datasource": Datasource.TEMPO.value,
                    "pivot_count": len(context_delta.pivots),
                    "time_window_count": len(context_delta.time_windows),
                },
            )
            data, truncated = compact_data(trace_data, self.max_output_chars)
            return ToolResult(
                datasource=Datasource.TEMPO,
                query=validated.trace_id,
                status=ResultStatus.SUCCESS,
                data=data,
                truncated=truncated,
                context_delta=context_delta,
            )
        except Exception as exc:
            error_detail, error_fields = describe_exception(exc)
            logger.error(
                "datasource.request.failed",
                extra={
                    "event": "datasource.request.failed",
                    "function_name": "TempoTool.execute",
                    "datasource": Datasource.TEMPO.value,
                    "url": self.url,
                    "query": trace_id,
                    **error_fields,
                },
                exc_info=True,
            )
            return ToolResult(
                datasource=Datasource.TEMPO,
                query=trace_id,
                status=ResultStatus.ERROR,
                error=f"Tempo 조회 실패: {error_detail}",
            )
