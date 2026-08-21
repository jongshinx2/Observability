import re

import httpx
from pydantic import BaseModel, Field, field_validator

from ..models import Datasource, ResultStatus, ToolResult
from ..observability import get_logger, trace_async
from .base import compact_data, describe_exception


logger = get_logger("prometheus_tool")


class PrometheusQuery(BaseModel):
    promql: str = Field(max_length=2000)

    @field_validator("promql")
    @classmethod
    def validate_promql(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("promql은 비어 있을 수 없습니다.")
        if "|" in value:
            raise ValueError(
                "PromQL에는 파이프('|')를 사용할 수 없습니다. "
                "예: http_requests_total{service_name=\"payment-api\"}"
            )
        if re.match(r"^[a-zA-Z_:][a-zA-Z0-9_:.]*\s*=(?!=)", value):
            raise ValueError(
                "PromQL은 label=값으로 시작할 수 없습니다. "
                "메트릭 이름 뒤 중괄호에 selector를 작성하세요."
            )
        return value


class PrometheusTool:
    def __init__(self, url: str, timeout: float, max_output_chars: int):
        self.url = url
        self.timeout = timeout
        self.max_output_chars = max_output_chars

    @trace_async("prometheus_tool")
    async def execute(self, query: str) -> ToolResult:
        try:
            validated = PrometheusQuery(promql=query)
            logger.info(
                "datasource.request.started",
                extra={
                    "event": "datasource.request.started",
                    "function_name": "PrometheusTool.execute",
                    "datasource": Datasource.PROMETHEUS.value,
                    "url": self.url,
                    "query": validated.promql,
                },
            )
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(self.url, params={"query": validated.promql})
            response.raise_for_status()
            records = response.json().get("data", {}).get("result", [])
            logger.info(
                "datasource.response.received",
                extra={
                    "event": "datasource.response.received",
                    "function_name": "PrometheusTool.execute",
                    "datasource": Datasource.PROMETHEUS.value,
                    "http_status": response.status_code,
                    "result_count": len(records),
                },
            )
            if not records:
                return ToolResult(
                    datasource=Datasource.PROMETHEUS,
                    query=validated.promql,
                    status=ResultStatus.EMPTY,
                    data=[],
                )
            data, truncated = compact_data(records, self.max_output_chars)
            return ToolResult(
                datasource=Datasource.PROMETHEUS,
                query=validated.promql,
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
                    "function_name": "PrometheusTool.execute",
                    "datasource": Datasource.PROMETHEUS.value,
                    "url": self.url,
                    "query": query,
                    **error_fields,
                },
                exc_info=True,
            )
            return ToolResult(
                datasource=Datasource.PROMETHEUS,
                query=query,
                status=ResultStatus.ERROR,
                error=f"Prometheus 조회 실패: {error_detail}",
            )
