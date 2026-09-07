from typing import Any

import yaml

from ..agent.llm import request_function_arguments
from ..config.settings import load_prompt
from ..models import Datasource, GeneratedQuery, InvestigationStep
from ..llm.options import LLMRequestOptions
from ..tools.prometheus import PrometheusQuery
from ..observability import get_logger, trace_async


logger = get_logger("prometheus_generator")


class PrometheusQueryGenerator:
    def __init__(
        self, client: Any, model: str, datasource_config: dict,
        request_options: LLMRequestOptions | None = None,
    ):
        self.client = client
        self.model = model
        self.request_options = request_options or LLMRequestOptions(max_tokens=512)
        self.system_prompt = (
            f"{load_prompt('prometheus')}\n\n"
            f"[Prometheus 환경 정보]\n{yaml.safe_dump(datasource_config, allow_unicode=True)}"
        )

    @trace_async("prometheus_generator")
    async def generate(self, step: InvestigationStep) -> GeneratedQuery:
        template_query = self._template_query(step)
        if template_query:
            logger.info(
                "query.generated_from_template",
                extra={
                    "event": "query.generated_from_template",
                    "function_name": "PrometheusQueryGenerator.generate",
                    "datasource": Datasource.PROMETHEUS.value,
                    "query": template_query,
                },
            )
            return GeneratedQuery(
                datasource=Datasource.PROMETHEUS,
                query=template_query,
                purpose=step.intent,
                time_range_minutes=step.time_range_minutes,
            )

        arguments = await request_function_arguments(
            client=self.client,
            model=self.model,
            request_options=self.request_options,
            system_prompt=self.system_prompt,
            user_prompt=step.model_dump_json(exclude_none=True),
            function_name="submit_prometheus_query",
            function_description="조사 의도에 맞는 PromQL 하나를 제출합니다.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "purpose": {"type": "string", "minLength": 1, "maxLength": 500},
                },
                "required": ["query", "purpose"],
            },
        )
        query = PrometheusQuery(promql=arguments.get("query", "")).promql
        logger.info(
            "query.generated_by_llm",
            extra={
                "event": "query.generated_by_llm",
                "function_name": "PrometheusQueryGenerator.generate",
                "datasource": Datasource.PROMETHEUS.value,
                "query": query,
                "model": self.model,
            },
        )
        return GeneratedQuery(
            datasource=Datasource.PROMETHEUS,
            query=query,
            purpose=arguments.get("purpose", step.intent),
            time_range_minutes=step.time_range_minutes,
        )

    @staticmethod
    def _template_query(step: InvestigationStep) -> str | None:
        intent = step.intent.lower()
        if step.service:
            service = step.service.replace("\\", "\\\\").replace('"', '\\"')
            if any(keyword in intent for keyword in ("오류", "에러", "5xx")):
                return (
                    f'http_requests_total{{service_name="{service}",'
                    'http_status_code=~"5.."}'
                )
            if any(keyword in intent for keyword in ("요청", "http", "상태")):
                return f'http_requests_total{{service_name="{service}"}}'
        if any(keyword in intent for keyword in ("수집", "collector", "프로메테우스")):
            return 'up{job="otel-collector"}'
        return None
