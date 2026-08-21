from datetime import datetime, timedelta, timezone

import httpx
from pydantic import BaseModel, Field, field_validator

from ..models import Datasource, ResultStatus, ToolResult
from ..observability import get_logger, trace_async
from .base import compact_data, describe_exception


logger = get_logger("loki_tool")


class LokiQuery(BaseModel):
    logql: str = Field(max_length=2000)

    @field_validator("logql")
    @classmethod
    def validate_logql(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("logql은 비어 있을 수 없습니다.")
        if not value.startswith("{"):
            raise ValueError("LogQL은 스트림 selector인 '{...}'로 시작해야 합니다.")
        return value


class LokiTool:
    def __init__(self, url: str, timeout: float, max_output_chars: int):
        self.url = url
        self.timeout = timeout
        self.max_output_chars = max_output_chars

    @trace_async("loki_tool")
    async def execute(self, query: str, time_range_minutes: int, limit: int) -> ToolResult:
        try:
            validated = LokiQuery(logql=query)
            end_time = datetime.now(timezone.utc)
            start_time = end_time - timedelta(minutes=time_range_minutes)
            params = {
                "query": validated.logql,
                "limit": limit,
                "start": str(int(start_time.timestamp() * 1e9)),
                "end": str(int(end_time.timestamp() * 1e9)),
            }
            logger.info(
                "datasource.request.started",
                extra={
                    "event": "datasource.request.started",
                    "function_name": "LokiTool.execute",
                    "datasource": Datasource.LOKI.value,
                    "url": self.url,
                    "query": validated.logql,
                    "time_range_minutes": time_range_minutes,
                    "limit": limit,
                },
            )
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(self.url, params=params)
            response.raise_for_status()
            streams = response.json().get("data", {}).get("result", [])
            logs = [value[1] for stream in streams for value in stream.get("values", [])]
            logger.info(
                "datasource.response.received",
                extra={
                    "event": "datasource.response.received",
                    "function_name": "LokiTool.execute",
                    "datasource": Datasource.LOKI.value,
                    "http_status": response.status_code,
                    "stream_count": len(streams),
                    "result_count": len(logs),
                },
            )
            if not logs:
                return ToolResult(
                    datasource=Datasource.LOKI,
                    query=validated.logql,
                    status=ResultStatus.EMPTY,
                    data=[],
                )
            data, truncated = compact_data(logs, self.max_output_chars)
            return ToolResult(
                datasource=Datasource.LOKI,
                query=validated.logql,
                status=ResultStatus.SUCCESS,
                data=data,
                truncated=truncated,
            )
        except Exception as exc:
            error_detail, error_fields = describe_exception(exc)
            logger.error(
                "datasource.request.failed",
                extra={
                    "event": "datasource.request.failed",
                    "function_name": "LokiTool.execute",
                    "datasource": Datasource.LOKI.value,
                    "url": self.url,
                    "query": query,
                    "time_range_minutes": time_range_minutes,
                    "limit": limit,
                    **error_fields,
                },
                exc_info=True,
            )
            return ToolResult(
                datasource=Datasource.LOKI,
                query=query,
                status=ResultStatus.ERROR,
                error=f"Loki 조회 실패: {error_detail}",
            )
